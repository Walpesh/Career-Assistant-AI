"""Billing Module — проверка подписи вебхуков платёжных шлюзов.

Поддерживаются три провайдера (TASK «Integrate payment gateway webhook
handler (YooKassa / CloudPayments / Stripe) with idempotency key checks»).
Схема подписи у каждого своя, но результат у всех одинаковый — это
:func:`verify_signature`, возвращающая нормализованное событие либо ``None``.

=============  ==========================================================
Провайдер      Алгоритм
=============  ==========================================================
yookassa       HMAC-SHA256 от тела запроса ключом из панели шлюза;
               заголовок ``X-Signature`` (формат ``sha256=<hex>``)
cloudpayments  HMAC от тела запроса ключом API-ключа; заголовок
               ``Content-HMAC`` (``sha1=<hex>`` либо ``sha256=<hex>``)
stripe         Подпись ``t=…,v1=…`` в заголовке ``Stripe-Signature``:
               HMAC-SHA256 от ``"<t>.<body>"``, сравнение константное,
               допуск окна расхождения времени (защита от replay)
=============  ==========================================================

Почему сравнение константное: обычный ``==`` по строке сравнивает
символы по порядку и позволяет по времени ответа перебирать хэш
байт за байтом. Для подписи платежа это позволяло бы подделать
оплату, поэтому используется :func:`hmac.compare_digest`.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass

from app.core.config import settings

__all__ = [
    "PROVIDERS",
    "PaymentEventData",
    "hmac_sha256",
    "verify_signature",
]

#: Поддерживаемые провайдеры → имя в БД-журнале `payment_events`.
PROVIDERS: frozenset[str] = frozenset({"yookassa", "cloudpayments", "stripe"})


@dataclass(frozen=True)
class PaymentEventData:
    """Нормализованное событие оплаты, одинаковое для всех провайдеров."""

    #: Идентификатор события у шлюза — ключ идемпотентности.
    external_event_id: str
    #: Тип события в нормализованном виде: payment.succeeded | payment.failed |
    #: payment.refunded | subscription.canceled | subscription.updated.
    event_type: str
    #: Тариф, который нужно выставить (None — тариф не меняется).
    tier: str | None = None
    #: Идентификатор платежа/подписки у шлюза (для сверки).
    external_id: str | None = None
    #: Конец оплаченного периода (метка времени), если шлюз его прислал.
    period_end: str | None = None
    #: Оригинальное тело события — сохраняется в `payment_events.payload`.
    payload: dict | None = None


def hmac_sha256(body: bytes, secret: str) -> str:
    """HEX-хэш HMAC-SHA256 тела запроса ключом шлюза."""
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def _safe_equal(left: str, right: str) -> bool:
    """Сравнение подписи за постоянное время (не даёт перебирать хэш)."""
    return hmac.compare_digest(left or "", right or "")


def _safe_equal_hex(left: str, right: str) -> bool:
    """Сравнение hex-подписей в байтовом виде (фиксированная длина)."""
    try:
        return hmac.compare_digest(bytes.fromhex(left), bytes.fromhex(right))
    except ValueError:
        return False


# --- YooKassa ------------------------------------------------------------------


def _verify_yookassa(body: bytes, secret: str, header: str) -> bool:
    """Подпись YooKassa: ``X-Signature: sha256=<hex>`` от тела запроса."""
    provided = (header or "").strip()
    if provided.lower().startswith("sha256="):
        provided = provided[7:]
    if not provided:
        return False
    return _safe_equal_hex(provided, hmac_sha256(body, secret))


def _normalize_yookassa_event(event_type: str) -> str:
    """YooKassa → внутренние имена событий."""
    mapping = {
        "payment.succeeded": "payment.succeeded",
        "payment.canceled": "payment.failed",
        "refund.created": "payment.refunded",
        "payment.refunded": "payment.refunded",
    }
    return mapping.get(event_type, event_type)


def parse_yookassa(payload: dict) -> PaymentEventData | None:
    """Событие YooKassa (``payment.succeeded`` / ``payment.canceled``)."""
    event_type = str(payload.get("event") or payload.get("type") or "")
    payment = payload.get("object")
    payment = payment if isinstance(payment, dict) else {}
    external_id = str(payment.get("id") or payload.get("payment_id") or "")
    if not event_type or not external_id:
        return None

    metadata = payment.get("metadata") or payload.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    # `tier` приходит в metadata, если продавец передал его при создании платежа.
    tier = metadata.get("tier") or payload.get("tier")
    return PaymentEventData(
        external_event_id=f"{event_type}:{external_id}",
        event_type=_normalize_yookassa_event(event_type),
        tier=str(tier) if tier else None,
        external_id=external_id,
        payload=payload,
    )


# --- CloudPayments -------------------------------------------------------------


def _verify_cloudpayments(body: bytes, secret: str, header: str) -> bool:
    """Подпись CloudPayments: ``Content-HMAC: sha1=<hex>`` от тела запроса."""
    provided = (header or "").strip()
    # CloudPayments исторически использует HMAC-SHA1; принимаем и SHA-256,
    # если шлюз прислал именно его (часть интеграций переключается).
    digestmod = hashlib.sha256
    if provided.lower().startswith("sha1="):
        provided = provided[5:]
        digestmod = hashlib.sha1
    elif provided.lower().startswith("sha256="):
        provided = provided[7:]
    if not provided:
        return False
    expected = hmac.new(secret.encode("utf-8"), body, digestmod).hexdigest()
    return _safe_equal_hex(provided, expected)


def parse_cloudpayments(payload: dict) -> PaymentEventData | None:
    """Событие CloudPayments (уведомление о платеже/подписке)."""
    status = str(payload.get("Status") or payload.get("status") or "")
    if not status:
        return None
    payment_id = str(payload.get("PaymentId") or payload.get("payment_id") or "")
    subscription_id = str(payload.get("SubscriptionId") or payload.get("subscription_id") or "")
    external_id = payment_id or subscription_id
    if not external_id:
        return None

    normalized = status.upper()
    if normalized == "COMPLETED":
        internal = "payment.succeeded"
    elif normalized in {"DECLINED", "FAILED", "CANCELLED"}:
        internal = "payment.failed"
    elif normalized == "VOIDED":
        internal = "payment.refunded"
    else:
        internal = f"payment.{normalized.lower()}"

    tier = payload.get("Tier") or payload.get("tier")
    return PaymentEventData(
        external_event_id=f"{internal}:{external_id}",
        event_type=internal,
        tier=str(tier) if tier else None,
        external_id=external_id,
        payload=payload,
    )


# --- Stripe --------------------------------------------------------------------


def _verify_stripe(body: bytes, secret: str, header: str, *, now: float | None = None) -> bool:
    """Подпись Stripe: ``Stripe-Signature: t=…,v1=…``.

    Проверяются и подпись, и что время события не старше окна
    ``BILLING_WEBHOOK_MAX_AGE_SECONDS`` — иначе перехваченный вебхук
    можно было бы переиграть позднее.
    """
    timestamp = ""
    signatures: list[str] = []
    for part in (header or "").split(","):
        key, _, value = part.strip().partition("=")
        if key == "t":
            timestamp = value
        elif key == "v1":
            signatures.append(value)
    if not timestamp or not signatures:
        return False

    try:
        event_time = int(timestamp)
    except ValueError:
        return False

    current = time.time() if now is None else now
    if abs(current - event_time) > settings.billing_webhook_max_age_seconds:
        return False

    expected = hmac_sha256(f"{timestamp}.".encode() + body, secret)
    return any(_safe_equal_hex(signature, expected) for signature in signatures)


#: Stripe → внутренние имена событий.
_STRIPE_EVENTS = {
    "checkout.session.completed": "payment.succeeded",
    "invoice.payment_succeeded": "payment.succeeded",
    "invoice.payment_failed": "payment.failed",
    "charge.refunded": "payment.refunded",
    "customer.subscription.deleted": "subscription.canceled",
    "customer.subscription.updated": "subscription.updated",
}


def parse_stripe(payload: dict) -> PaymentEventData | None:
    """Событие Stripe (``checkout.session.completed``, ``customer.subscription.*``)."""
    event_type = str(payload.get("type") or "")
    data = payload.get("data")
    data_object = data.get("object") if isinstance(data, dict) else None
    data_object = data_object if isinstance(data_object, dict) else {}
    external_event_id = str(payload.get("id") or "")
    if not event_type or not external_event_id:
        return None

    metadata = data_object.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    tier = metadata.get("tier")
    period_end = data_object.get("current_period_end")
    return PaymentEventData(
        external_event_id=external_event_id,
        event_type=_STRIPE_EVENTS.get(event_type, event_type),
        tier=str(tier) if tier else None,
        external_id=str(data_object.get("id") or data_object.get("subscription") or "") or None,
        period_end=str(period_end) if period_end else None,
        payload=payload,
    )


# --- диспетчер -----------------------------------------------------------------

#: Провайдер → (верификатор подписи, парсер тела).
_HANDLERS = {
    "yookassa": (_verify_yookassa, parse_yookassa),
    "cloudpayments": (_verify_cloudpayments, parse_cloudpayments),
    "stripe": (_verify_stripe, parse_stripe),
}

#: Провайдер → заголовки с подписью (регистр заголовков не важен).
_SIGNATURE_HEADERS = {
    "yookassa": ("x-signature",),
    "cloudpayments": ("content-hmac", "x-hmac"),
    "stripe": ("stripe-signature",),
}


def _signature_header(provider: str, headers) -> str:
    """Значение заголовка с подписью для конкретного провайдера."""
    lookup = {str(key).lower(): value for key, value in (headers or {}).items()}
    for name in _SIGNATURE_HEADERS[provider]:
        value = lookup.get(name)
        if value:
            return str(value)
    return ""


def verify_signature(
    provider: str, body: bytes, headers, *, secret: str | None = None
) -> PaymentEventData | None:
    """Проверить подпись вебхука и разобрать его в нормализованное событие.

    Args:
        provider: ``yookassa`` / ``cloudpayments`` / ``stripe``.
        body: сырое тело запроса — подпись считается именно по нему, а не по
            пересобранному JSON: у шлюзов каноническая сериализация.
        headers: заголовки запроса (для поиска подписи).
        secret: секрет шлюза; по умолчанию ``BILLING_WEBHOOK_SECRET``.

    Returns:
        Нормализованное событие либо ``None``, если подпись не сошлась,
        провайдер неизвестен, секрет не настроен либо тело не разобралось.
        Вызывающий код превращает ``None`` в 400/401: «пропустить событие
        молча» здесь означало бы принять неоплаченный платёж.
    """
    normalized = (provider or "").strip().lower()
    if normalized not in PROVIDERS:
        return None

    key = settings.billing_webhook_secret if secret is None else secret
    if not key:
        # Секрет не настроен → подпись проверить нечем. Принять событие значило
        # бы разрешить любому отправителю выдать себя за платёжный шлюз.
        return None

    verifier, parser = _HANDLERS[normalized]
    if not verifier(body, key, _signature_header(normalized, headers)):
        return None

    try:
        payload = json.loads(body or b"{}")
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None

    try:
        return parser(payload)
    except Exception:  # noqa: BLE001 — кривой payload не должен ронять сервис
        return None
    except ValueError:
        return False
