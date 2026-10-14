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

import base64
import hashlib
import hmac
import json
import time
import uuid
from dataclasses import dataclass

import httpx

from app.core.config import settings
from app.core.errors import AppError

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

#: Провайдер → заголовок с подписью.
_SIGNATURE_HEADERS = {
    "yookassa": "X-Signature",
    "cloudpayments": "Content-HMAC",
    "stripe": "Stripe-Signature",
}


def _header(headers, name: str) -> str:
    """Достать заголовок из маппинга (dict) или starlette Headers.

    Starlette-Headers уже case-insensitive; для обычного dict делаем
    поиск без учёта регистра, чтобы не зависеть от написания ключа.
    """
    getter = getattr(headers, "get", None)
    if getter is not None:
        value = getter(name)
        if value:
            return str(value)
    try:
        items = headers.items()
    except AttributeError:
        return ""
    lowered = name.lower()
    for key, value in items:
        if str(key).lower() == lowered and value:
            return str(value)
    return ""


def verify_signature(
    provider: str,
    body: bytes,
    headers,
    *,
    secret: str | None = None,
) -> PaymentEventData | None:
    """Проверить подпись вебхука и вернуть нормализованное событие.

    Аргументы:
        provider: Имя провайдера (``yookassa`` | ``cloudpayments`` | ``stripe``).
        body: Сырое тело запроса (подпись считается именно от него).
        headers: Маппинг заголовков (dict или starlette Headers).
        secret: Ключ шлюза; если не передан — ``settings.billing_webhook_secret``,
            а для Stripe дополнительно ``settings.stripe_webhook_secret``
            (набор ключей только расширяется: принимавшиеся подписи
            остаются принятыми).

    Возвращает:
        Нормализованное событие либо ``None``, если подпись неверна,
        секрет не настроен или провайдер не поддерживается.
    """
    normalized = (provider or "").strip().lower()
    handler = _HANDLERS.get(normalized)
    if handler is None:
        return None
    verify, parse = handler

    if secret is not None:
        keys = [secret]
    else:
        keys = [settings.billing_webhook_secret]
        if normalized == "stripe":
            keys.append(settings.stripe_webhook_secret)

    provided = _header(headers, _SIGNATURE_HEADERS[normalized])
    if not provided or not any(key and verify(body, key, provided) for key in keys):
        return None

    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    return parse(payload)


# --- Payment sessions (checkout) ----------------------------------------------

GATEWAY_BASE_URL: dict[str, str] = {
    "yookassa": "https://api.yookassa.ru/v3",
    "cloudpayments": "https://api.cloudpayments.ru",
    "stripe": "https://api.stripe.com",
}

GATEWAY_PAYMENT_PATH: dict[str, str] = {
    "yookassa": "/v3/payments",
    "cloudpayments": "/payments",
    "stripe": "/v1/checkout/sessions",
}


def _kopeks(value: object) -> int:
    """Convert a price (rubles) into integer kopecks."""
    return int(round(float(value or 0.0) * 100))


def _cloudpayments_signature(
    shop_id: str,
    payment_type: str,
    amount: str,
    currency: str,
    description: str,
    signature_version: str,
    redirect_url: str,
    server_url: str,
) -> str:
    """Sign a CloudPayments create-payment request (same algorithm as the gateway)."""
    message = f"{shop_id}{payment_type}{amount}{currency}{description}{signature_version}{redirect_url}{server_url}"
    return hmac.new(
        settings.cloudpayments_secret_key.encode("utf-8"),
        message.encode("utf-8"),
        hashlib.sha1,
    ).hexdigest().upper()


def _payment_metadata(user_id: uuid.UUID, tier: str) -> dict:
    """Gateway-visible metadata (user_id / tier) consumed by the webhook handler."""
    return {"user_id": str(user_id), "tier": tier}


async def create_payment_session(
    provider: str,
    user_id: uuid.UUID,
    tier: str,
    pricing: dict,
    return_url: str | None = None,
) -> dict:
    """Создать сессию оплаты в активном провайдере.

    Возвращает ``confirmation_url`` (адрес переадресации) и ``payment_id``.
    """
    provider = (provider or "").strip().lower()
    if provider not in PROVIDERS:
        raise AppError(
            400,
            f"Провайдер оплаты не поддерживается: {provider}",
            "PAYMENT_PROVIDER_INVALID",
        )

    amount_kopeks = _kopeks(pricing.get("price") if isinstance(pricing, dict) else pricing)
    metadata = _payment_metadata(user_id, tier)
    return_url = return_url or f"{settings.front_base_url}/pricing/success"

    if provider == "yookassa":
        return await _create_yookassa_payment(amount_kopeks, metadata, return_url, tier)
    if provider == "cloudpayments":
        return await _create_cloudpayments_payment(amount_kopeks, metadata, return_url, tier, user_id)
    if provider == "stripe":
        return await _create_stripe_checkout_session(amount_kopeks, metadata, return_url, tier)

    raise AppError(
        500,
        f"Не реализован способ создания сессии для провайдера: {provider}",
        "PAYMENT_PROVIDER_ERROR",
    )


async def _create_yookassa_payment(
    amount_kopeks: int,
    metadata: dict,
    return_url: str,
    tier: str,
) -> dict:
    if not (settings.yookassa_shop_id and settings.yookassa_secret_key):
        raise AppError(
            503,
            "YooKassa не настроен на сервере (SHOP_ID / SECRET_KEY)",
            "PAYMENT_GATEWAY_UNAVAILABLE",
        )
    auth = base64.b64encode(
        f"{settings.yookassa_shop_id}:{settings.yookassa_secret_key}".encode()
    ).decode("ascii")
    payload = {
        "amount": {"value": str(amount_kopeks), "currency": "RUB"},
        "confirmation": {"type": "redirect", "return_url": return_url},
        "description": f"Абонемент {tier.title()}",
        "metadata": metadata,
    }
    headers = {
        "Authorization": f"Basic {auth}",
        "Idempotence-Key": str(uuid.uuid4()),
        "Content-Type": "application/json",
    }
    url = f"{GATEWAY_BASE_URL['yookassa']}{GATEWAY_PAYMENT_PATH['yookassa']}"
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.post(url, json=payload, headers=headers)
    if response.status_code in (200, 201):
        data = response.json()
        confirmation = data.get("confirmation") or {}
        return {
            "confirmation_url": confirmation.get("confirmation_url"),
            "payment_id": data.get("id"),
        }
    detail = (response.json() or {}).get("description") or response.text[:300]
    raise AppError(502, f"YooKassa: {detail}", "PAYMENT_GATEWAY_ERROR")


async def _create_cloudpayments_payment(
    amount_kopeks: int,
    metadata: dict,
    return_url: str,
    tier: str,
    user_id: uuid.UUID,
) -> dict:
    if not (settings.cloudpayments_shop_id and settings.cloudpayments_secret_key):
        raise AppError(
            503,
            "CloudPayments не настроен на сервере (SHOP_ID / SECRET_KEY)",
            "PAYMENT_GATEWAY_UNAVAILABLE",
        )
    amount = str(amount_kopeks)
    shop_id = settings.cloudpayments_shop_id
    signature = _cloudpayments_signature(
        shop_id,
        "sale",
        amount,
        "RUB",
        f"Абонемент {tier}",
        "HmacSHA1",
        return_url,
        f"{settings.front_base_url}/api/v1/billing/return",
    )
    payload = {
        "shopId": shop_id,
        "paymentType": "sale",
        "amount": {"value": amount, "currency": "RUB"},
        "description": f"Абонемент {tier}",
        "redirectUrl": return_url,
        "serverUrl": f"{settings.front_base_url}/api/v1/billing/return",
        "customer": {"email": f"user-{user_id}@example.com"},
        "signature": signature,
        "signatureVersion": "HmacSHA1",
    }
    headers = {"Content-Type": "application/json"}
    url = f"{GATEWAY_BASE_URL['cloudpayments']}{GATEWAY_PAYMENT_PATH['cloudpayments']}"
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.post(url, json=payload, headers=headers)
    if response.status_code in (200, 201):
        data = response.json()
        payment = data.get("payment") or data
        return {
            "confirmation_url": payment.get("redirectUrl") or payment.get("payUrl") or "",
            "payment_id": payment.get("id") or payment.get("paymentId") or "",
        }
    detail = (response.json() or {}).get("message") or response.text[:300]
    raise AppError(502, f"CloudPayments: {detail}", "PAYMENT_GATEWAY_ERROR")


async def _create_stripe_checkout_session(
    amount_kopeks: int,
    metadata: dict,
    return_url: str,
    tier: str,
) -> dict:
    if not settings.stripe_secret_key:
        raise AppError(
            503,
            "Stripe не настроен на сервере (SECRET_KEY)",
            "PAYMENT_GATEWAY_UNAVAILABLE",
        )
    headers = {
        "Authorization": f"Bearer {settings.stripe_secret_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "mode": "payment",
        "line_items": [
            {
                "price_data": {
                    "currency": "rub",
                    "product_data": {"name": f"Абонемент {tier.title()}"},
                    "unit_amount": amount_kopeks,
                }
            }
        ],
        "success_url": (
            f"{settings.front_base_url}/pricing/success?session_id={{CHECKOUT_SESSION_ID}}"
        ),
        "cancel_url": f"{settings.front_base_url}/pricing",
        "metadata": metadata,
    }
    url = f"{GATEWAY_BASE_URL['stripe']}{GATEWAY_PAYMENT_PATH['stripe']}"
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.post(url, json=payload, headers=headers)
    if response.status_code in (200, 201):
        data = response.json()
        return {
            "confirmation_url": data.get("url"),
            "payment_id": data.get("id"),
        }
    detail = (response.json() or {}).get("error") or response.text[:300]
    raise AppError(502, f"Stripe: {detail}", "PAYMENT_GATEWAY_ERROR")
