"""Billing Module — тарифы, суточные квоты и платёжные вебхуки.

TASK «Monetization Engine & Quota Management» (docs/03 §11, docs/02 §3.8–§3.10):
    tiers.py     — каталог free / pro / enterprise и их суточные квоты;
    service.py   — расход квот (атомарно) и идемпотентные вебхуки;
    providers.py — проверка подписи YooKassa / CloudPayments / Stripe;
    middleware.py — зависимость FastAPI, начисляющая квоту до постановки
                    задачи в очередь;
    router.py    — эндпоинты /billing/tiers, /usage, /subscription, /webhook.
"""

from app.modules.billing.middleware import QuotaGuard, quota_consumed, require_quota
from app.modules.billing.providers import PROVIDERS, PaymentEventData, verify_signature
from app.modules.billing.router import router
from app.modules.billing.service import (
    QuotaExceeded,
    WebhookError,
    consume_quota,
    get_tier,
    process_webhook,
    quota_day,
    quota_reset_at,
    quota_states,
    quota_summary,
)
from app.modules.billing.tiers import (
    ENTERPRISE,
    FREE,
    PRO,
    TIERS,
    UNLIMITED,
    QuotaKind,
    QuotaState,
    TierQuota,
    available_tiers,
    quota_for,
    resolve_tier,
)

__all__ = [
    "ENTERPRISE",
    "FREE",
    "PRO",
    "PROVIDERS",
    "TIERS",
    "UNLIMITED",
    "PaymentEventData",
    "QuotaExceeded",
    "QuotaGuard",
    "QuotaKind",
    "QuotaState",
    "TierQuota",
    "WebhookError",
    "available_tiers",
    "consume_quota",
    "get_tier",
    "process_webhook",
    "quota_consumed",
    "quota_day",
    "quota_for",
    "quota_reset_at",
    "quota_states",
    "quota_summary",
    "require_quota",
    "resolve_tier",
    "router",
    "verify_signature",
]
