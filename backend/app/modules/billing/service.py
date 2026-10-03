"""Billing Module — сервис квот и обработки платёжных вебхуков.

Логика модуля (TASK «Monetization Engine & Quota Management»):

1. :func:`get_tier`      — тариф пользователя (нет подписки → ``free``);
2. :func:`quota_states`  — текущий расход по всем видам квот за сутки;
3. :func:`consume_quota` — **атомарное** начисление расхода с проверкой
   лимита в одной SQL-операции (ON CONFLICT DO UPDATE ... WHERE), поэтому
   два параллельных запроса не могут обойти лимит;
4. :func:`process_webhook` — идемпотентная обработка события платёжного
   шлюза: повторная доставка того же события не меняет тариф дважды.

Почему начисление атомарно, а не «прочитал → прибавил → записал»: два
одновременных POST /analysis/run на последнем свободном месте квоты
при чтении-изменении-записи оба увидели бы одно и то же значение и оба
прошли бы проверку. Условие ``WHERE`` внутри ``ON CONFLICT`` выполняется
в той же транзакции базы, поэтому лимит физически не превышается.

Сутки считаются по UTC: :func:`quota_day` возвращает календарную дату,
а :func:`quota_reset_at` — метку времени следующего сброса (для UI).
"""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.core.logging import get_logger
from app.db.models import PaymentEvent, Subscription, UsageCounter, User
from app.modules.billing.providers import PROVIDERS, PaymentEventData, verify_signature
from app.modules.billing.tiers import (
    UNLIMITED,
    QuotaKind,
    QuotaState,
    TierQuota,
    available_tiers,
    quota_for,
    resolve_tier,
)

__all__ = [
    "QuotaExceeded",
    "WebhookError",
    "apply_payment_event",
    "consume_quota",
    "get_tier",
    "process_webhook",
    "quota_day",
    "quota_reset_at",
    "quota_states",
    "quota_summary",
]

log = get_logger(__name__)


class QuotaExceeded(AppError):
    """Суточная квота тарифа исчерпана (HTTP 429, docs/03 §9)."""

    def __init__(self, kind: str, state: QuotaState, tier: str) -> None:
        super().__init__(
            429,
            (
                f"Исчерпана суточная квота «{kind}» для тарифа {tier}: "
                f"{state.used}/{state.limit}. Квота обновится "
                f"{state.resets_at}. Смена тарифа: GET /api/v1/billing/tiers"
            ),
            "QUOTA_EXCEEDED",
        )
        self.kind = kind
        self.tier = tier


class WebhookError(AppError):
    """Вебхук отклонён: подпись не сошлась или событие неприменимо."""

    def __init__(self, detail: str, error_code: str = "WEBHOOK_INVALID") -> None:
        super().__init__(400, detail, error_code)


# --- сутки ---------------------------------------------------------------------


def quota_day(moment: datetime | None = None) -> date:
    """Календарные сутки расхода в UTC (единственная точка вычисления дня)."""
    current = moment or datetime.now(UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    return current.astimezone(UTC).date()


def quota_reset_at(moment: datetime | None = None) -> str:
    """Метка времени следующего сброса квот (начало следующих суток UTC)."""
    day = quota_day(moment)
    return (datetime(day.year, day.month, day.day, tzinfo=UTC) + timedelta(days=1)).isoformat()


# --- тарифы и квоты ------------------------------------------------------------


async def get_tier(db: AsyncSession, user_id: uuid.UUID) -> str:
    """Тариф пользователя; нет подписки или она не активна → ``free``.

    Просроченная подписка (``status != 'active'``) намеренно **не** даёт
    прав: отменённая или просроченная шлюзом оплата не должна продлевать
    доступ к платным ресурсам.
    """
    subscription = await db.scalar(select(Subscription).where(Subscription.user_id == user_id))
    if subscription is None or subscription.status != "active":
        return "free"
    return resolve_tier(subscription.tier)


async def _effective_quotas(db: AsyncSession, user_id: uuid.UUID) -> tuple[str, TierQuota]:
    """Тариф и действующие квоты (с учётом индивидуальных переопределений).

    Переопределение в строке `subscriptions` важно для enterprise-контрактов:
    там лимиты часто фиксируют письменно, а не по каталогу тарифов.
    """
    subscription = await db.scalar(select(Subscription).where(Subscription.user_id == user_id))
    tier = resolve_tier(subscription.tier) if subscription is not None else "free"
    base = quota_for(tier)
    if subscription is None:
        return tier, base

    overrides = {
        QuotaKind.PARSE: subscription.daily_parsing_jobs,
        QuotaKind.LETTER: subscription.daily_cover_letters,
        QuotaKind.ANALYSIS: subscription.daily_analyses,
        QuotaKind.PROXY_MB: subscription.daily_proxy_mb,
    }
    if all(value is None for value in overrides.values()):
        return tier, base

    effective = replace(
        base,
        **{
            kind: (base.limit_for(kind) if value is None else int(value))
            for kind, value in overrides.items()
        },
    )
    return tier, effective


async def _usage_map(db: AsyncSession, user_id: uuid.UUID, day: date) -> dict[str, int]:
    """Расход пользователя за сутки: {quota_kind: used}."""
    rows = await db.execute(
        select(UsageCounter.quota_kind, UsageCounter.used).where(
            UsageCounter.user_id == user_id,
            UsageCounter.day == day,
        )
    )
    return {kind: int(used) for kind, used in rows.all()}


async def quota_states(db: AsyncSession, user_id: uuid.UUID) -> dict[str, QuotaState]:
    """Состояние всех суточных квот пользователя (для GET /billing/usage)."""
    _, quotas = await _effective_quotas(db, user_id)
    day = quota_day()
    used = await _usage_map(db, user_id, day)
    resets_at = quota_reset_at()
    return {
        kind: QuotaState(
            kind=kind,
            used=used.get(kind, 0),
            limit=quotas.limit_for(kind),
            resets_at=resets_at,
        )
        for kind in QuotaKind.ALL
    }


async def quota_summary(db: AsyncSession, user_id: uuid.UUID) -> dict:
    """Полная сводка: тариф, квоты, использованные и оставшиеся значения."""
    tier, quotas = await _effective_quotas(db, user_id)
    states = await quota_states(db, user_id)
    return {
        "tier": tier,
        "day": quota_day().isoformat(),
        "resets_at": quota_reset_at(),
        "quotas": {
            kind: {
                "used": state.used,
                "limit": state.limit,
                "remaining": state.remaining,
                "unlimited": state.unlimited,
                "exhausted": state.exhausted,
                "resets_at": state.resets_at,
            }
            for kind, state in states.items()
        },
        "unlimited": [kind for kind in QuotaKind.ALL if quotas.is_unlimited(kind)],
    }


# --- расход квот ---------------------------------------------------------------


async def consume_quota(
    db: AsyncSession,
    user_id: uuid.UUID,
    kind: str,
    amount: int = 1,
    *,
    enforce: bool | None = None,
) -> QuotaState | None:
    """Начислить расход по квоте, не превышая лимит тарифа.

    Args:
        kind: вид квоты (``QuotaKind``).
        amount: величина расхода (обычно 1; для прокси — МБ трафика).
        enforce: принудительное включение проверки лимита. ``None`` — берётся
            ``BILLING_ENABLED``, поэтому квоты выключаются одним флагом
            окружения, не меняя код.

    Returns:
        Состояние квоты после начисления либо ``None``, если контроль выключен.

    Raises:
        QuotaExceeded: лимит исчерпан (только при включённом контроле).
    """
    from app.core.config import settings

    checking = settings.billing_enabled if enforce is None else enforce
    if not checking:
        return None

    step = max(1, int(amount))
    tier, quotas = await _effective_quotas(db, user_id)
    limit = quotas.limit_for(kind)

    if limit == UNLIMITED:
        # Безлимитная квота всё равно пишется: иначе нельзя было бы показать
        # пользователю фактический расход в GET /billing/usage.
        used = await _increment(db, user_id, kind, step, enforce_limit=False)
        return await _state_for(db, user_id, kind, used=used)

    used = await _increment(db, user_id, kind, step, enforce_limit=True)
    if used is None:
        # Условие WHERE в ON CONFLICT не выполнилось → лимит превышен.
        raise QuotaExceeded(kind, await _state_for(db, user_id, kind, used=None), tier)
    return await _state_for(db, user_id, kind, used=used)


async def _increment(
    db: AsyncSession, user_id: uuid.UUID, kind: str, amount: int, *, enforce_limit: bool
) -> int | None:
    """Инкремент счётчика за сутки; ``None`` — лимит превышен.

    При ``enforce_limit`` ограничение накладывается условием самого INSERT'а,
    поэтому конкурентные запросы физически не могут пройти вместе.
    """
    _, quotas = await _effective_quotas(db, user_id)
    limit = quotas.limit_for(kind)

    statement = (
        pg_insert(UsageCounter)
        .values(user_id=user_id, day=quota_day(), quota_kind=kind, used=amount)
        .on_conflict_do_update(
            index_elements=[
                UsageCounter.user_id,
                UsageCounter.day,
                UsageCounter.quota_kind,
            ],
            set_={"used": UsageCounter.used + amount},
            where=(UsageCounter.used + amount <= limit) if enforce_limit else None,
        )
        .returning(UsageCounter.used)
    )
    try:
        value = await db.scalar(statement)
        await db.commit()
    except IntegrityError as exc:  # pragma: no cover — защита от гонок БД
        await db.rollback()
        log.warning("usage_counter integrity error", error_type=type(exc).__name__)
        return None

    return None if value is None else int(value)


async def _state_for(
    db: AsyncSession, user_id: uuid.UUID, kind: str, used: int | None
) -> QuotaState:
    """Текущее состояние одной квоты (обычно — сразу после начисления)."""
    _, quotas = await _effective_quotas(db, user_id)
    if used is None:
        counters = await _usage_map(db, user_id, quota_day())
        used = counters.get(kind, 0)
    return QuotaState(
        kind=kind,
        used=used,
        limit=quotas.limit_for(kind),
        resets_at=quota_reset_at(),
    )


# --- обработка платёжных вебхуков ---------------------------------------------


def _user_id_from_metadata(payload: dict | None) -> uuid.UUID | None:
    """Идентификатор пользователя из тела события оплаты.

    Расположение метаданных различается у шлюзов, поэтому проверяются все
    известные варианты:
      * YooKassa — ``object.metadata.user_id``;
      * CloudPayments — ``user_id`` в корне;
      * Stripe — ``data.object.metadata.user_id``.

    Если ``user_id`` не найден, тариф не применяется: придумывать привязку
    по email из платежа означало бы риск выдать чужую подписку.
    """
    if not isinstance(payload, dict):
        return None

    candidates: list[object] = [payload.get("user_id")]

    # YooKassa/CloudPayments кладут тариф и пользователя в object.metadata.
    payment = payload.get("object")
    if isinstance(payment, dict):
        candidates.append(payment.get("user_id"))
        candidates.append(payment.get("CustomerId"))

    # Stripe: data.object.metadata.
    data = payload.get("data")
    data_object = data.get("object") if isinstance(data, dict) else None
    if isinstance(data_object, dict):
        candidates.append(data_object.get("user_id"))

    for holder in (payload, payment if isinstance(payment, dict) else None, data_object):
        if not isinstance(holder, dict):
            continue
        metadata = holder.get("metadata")
        if isinstance(metadata, dict):
            candidates.append(metadata.get("user_id"))

    for raw in candidates:
        if raw:
            try:
                return uuid.UUID(str(raw))
            except (ValueError, TypeError, AttributeError):
                continue
    return None


def _parse_period_end(event: PaymentEventData) -> datetime | None:
    """Метка конца оплаченного периода (Stripe присылает Unix-время)."""
    if not event.period_end:
        return None
    try:
        seconds = int(event.period_end)
    except (TypeError, ValueError):
        return None
    try:
        return datetime.fromtimestamp(seconds, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


async def apply_payment_event(
    db: AsyncSession, event: PaymentEventData, provider: str
) -> Subscription | None:
    """Применить событие оплаты к подписке пользователя.

    Идемпотентность обеспечивает вызывающий код (:func:`process_webhook`),
    который сначала атомарно резервирует ``(provider, external_event_id)``
    в `payment_events`. Здесь меняется только сама подписка.
    """
    user_id = _user_id_from_metadata(event.payload)
    if user_id is None:
        log.warning(
            "billing: в событии оплаты нет user_id — тариф не применён",
            event_type=event.event_type,
            provider=provider,
        )
        return None

    user = await db.get(User, user_id)
    if user is None:
        log.warning(
            "billing: пользователь события оплаты не найден",
            event_type=event.event_type,
            provider=provider,
        )
        return None

    subscription = await db.scalar(select(Subscription).where(Subscription.user_id == user_id))
    if subscription is None:
        subscription = Subscription(user_id=user_id)
        db.add(subscription)

    subscription.provider = provider
    subscription.external_id = event.external_id
    if event.tier:
        subscription.tier = resolve_tier(event.tier)

    if event.event_type == "payment.succeeded":
        subscription.status = "active"
        period_end = _parse_period_end(event)
        if period_end is not None:
            subscription.current_period_end = period_end
    elif event.event_type == "subscription.canceled":
        # Отмена не отбирает оплаченный период: доступ сохраняется до конца
        # оплаченного срока, затем тариф вернётся в free.
        subscription.status = "canceled"
        subscription.canceled_at = datetime.now(UTC)
    elif event.event_type == "payment.failed":
        subscription.status = "past_due"

    await db.commit()
    await db.refresh(subscription)
    log.info(
        "billing: подписка обновлена",
        event_type=event.event_type,
        provider=provider,
        status=subscription.status,
    )
    return subscription


async def _claim_event(
    db: AsyncSession, provider: str, event: PaymentEventData, user_id: uuid.UUID | None
) -> bool:
    """Атомно занять ключ идемпотентности.

    Returns:
        True — событие обрабатывает этот запрос; False — дубликат. Вставка
        опирается на UNIQUE ``(provider, external_event_id)``, поэтому два
        одновременных повтора одного события не пройдут оба.
    """
    statement = (
        pg_insert(PaymentEvent)
        .values(
            provider=provider,
            external_event_id=event.external_event_id,
            event_type=event.event_type,
            user_id=user_id,
            tier=resolve_tier(event.tier) if event.tier else None,
            status="processed",
            payload=event.payload,
        )
        .on_conflict_do_nothing(index_elements=["provider", "external_event_id"])
        .returning(PaymentEvent.id)
    )
    claimed = await db.scalar(statement)
    await db.commit()
    return claimed is not None


async def process_webhook(db: AsyncSession, provider: str, body: bytes, headers) -> dict:
    """Проверить подпись и идемпотентно применить событие платёжного шлюза.

    Returns:
        ``{"status": "processed" | "duplicate" | "ignored", ...}``.

    Raises:
        WebhookError: неизвестный провайдер, неверная подпись, секрет не
            настроен либо тело не разобрано. Ни один из этих случаев не
            должен «проходить молча»: иначе атакующий отправлял бы пустые
            вебхуки и получал бы 200, считая оплату успешной.
    """
    normalized = (provider or "").strip().lower()
    if normalized not in PROVIDERS:
        raise WebhookError(
            f"Неизвестный платёжный провайдер: {provider!r}. "
            f"Поддерживаются: {', '.join(sorted(PROVIDERS))}",
            "WEBHOOK_PROVIDER_UNSUPPORTED",
        )

    event = verify_signature(normalized, body, headers)
    if event is None:
        raise WebhookError(
            "Подпись вебхука не прошла проверку (или BILLING_WEBHOOK_SECRET "
            "не настроен) — событие отклонено",
            "WEBHOOK_SIGNATURE_INVALID",
        )

    user_id = _user_id_from_metadata(event.payload)
    if not await _claim_event(db, normalized, event, user_id):
        # Повторная доставка: платёж уже учтён, второй раз тариф не меняем.
        log.info(
            "billing: дубликат вебхука проигнорирован",
            event_type=event.event_type,
            provider=normalized,
        )
        return {
            "status": "duplicate",
            "event_type": event.event_type,
            "provider": normalized,
            "external_event_id": event.external_event_id,
        }

    subscription = await apply_payment_event(db, event, normalized)
    return {
        "status": "processed" if subscription is not None else "ignored",
        "event_type": event.event_type,
        "provider": normalized,
        "external_event_id": event.external_event_id,
        "tier": subscription.tier if subscription is not None else None,
    }


def tiers_catalog() -> list[dict]:
    """Каталог тарифов для GET /billing/tiers (delegates в tiers.py)."""
    return available_tiers()
