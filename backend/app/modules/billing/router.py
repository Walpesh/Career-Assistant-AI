"""Billing Module — endpoints (docs/03_API_CONTRACTS.md §11).

Контракт:
    GET  /billing/tiers             — каталог тарифов и их суточные квоты;
    GET  /billing/usage             — текущий тариф и расход квот за сутки;
    GET  /billing/subscription      — состояние подписки пользователя;
    POST /billing/webhook/{provider} — приём платёжного вебхука (без Bearer).

Вебхук — единственный публичный (не требующий авторизации) эндпоинт
Billing Module: подпись проверяется в ``billing.providers``, а повторная
доставка отсекается ключом идемпотентности ``payment_events``.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.errors import AppError
from app.db.models import Subscription, User
from app.db.session import get_db
from app.modules.auth.deps import get_current_user
from app.modules.billing.providers import PROVIDERS
from app.modules.billing.service import (
    get_tier,
    process_webhook,
    quota_summary,
    tiers_catalog,
)

__all__ = ["router"]

router = APIRouter(prefix="/billing", tags=["billing"])


@router.get("/tiers", summary="Каталог тарифов и суточных квот (docs/03 §11)")
async def list_tiers() -> dict:
    """Публичный каталог: тарифы, лимиты и виды безлимитных квот.

    Открыт без авторизации — пользователь должен видеть, что даёт смена
    тарифа, ещё до оплаты.
    """
    return {
        "billing_enabled": settings.billing_enabled,
        "provider": settings.billing_provider,
        "providers": sorted(PROVIDERS),
        "tiers": tiers_catalog(),
    }


@router.get("/usage", summary="Текущий тариф и расход суточных квот (docs/03 §11)")
async def usage(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict:
    """Сколько квот израсходовано сегодня и когда они обновятся."""
    return await quota_summary(db, user.id)


@router.get("/subscription", summary="Состояние подписки (docs/03 §11)")
async def subscription(
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> dict:
    """Тариф, статус и период подписки; без подписки — эффективный free."""
    row = await db.scalar(select(Subscription).where(Subscription.user_id == user.id))
    return {
        "tier": await get_tier(db, user.id),
        "status": row.status if row is not None else "active",
        "provider": row.provider if row is not None else None,
        "external_id": row.external_id if row is not None else None,
        "current_period_end": row.current_period_end if row is not None else None,
        "canceled_at": row.canceled_at if row is not None else None,
    }


@router.post(
    "/webhook/{provider}",
    summary="Вебхук платёжного шлюза (yookassa/cloudpayments/stripe)",
)
async def payment_webhook(
    provider: str, request: Request, db: AsyncSession = Depends(get_db)
) -> dict:
    """Принять событие оплаты: проверить подпись и применить один раз.

    Тело читается целиком и ограничивается ``BILLING_WEBHOOK_MAX_BODY_BYTES``:
    подпись считается по сырым байтам, а не по пересобранному JSON.
    """
    body = await request.body()
    limit = max(1024, settings.billing_webhook_max_body_bytes)
    if len(body) > limit:
        raise AppError(
            413,
            f"Тело вебхука превышает {limit} байт",
            "WEBHOOK_BODY_TOO_LARGE",
        )

    result = await process_webhook(db, provider, body, dict(request.headers))
    # Ответ всегда 200: неподтверждённый вебхук шлюз будет повторять, а
    # повтор безопасен — сработает ключ идемпотентности.
    return result
