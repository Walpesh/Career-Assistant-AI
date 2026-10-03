"""Privacy Module — endpoints (docs/03_API_CONTRACTS.md §10).

Контракт:
    DELETE /api/v1/account         — каскадное удаление аккаунта и всех данных;
    GET  /api/v1/account/export    — полная выгрузка персональных данных (JSON);
    GET  /api/v1/account/summary   — что будет удалено (сводка до удаления).

Основание: 152-ФЗ ст. 14 (право на доступ) и ст. 21 (право на удаление),
GDPR ст. 15 и 17. Оба эндпоинта требуют Bearer JWT — анонимно удалить
или выгрузить данные нельзя (docs/03 §1 «Auth: Да»).

Про ``GET /api/v1/account/export``:
    * ``Content-Disposition: attachment`` — ответ предлагается сохранить как
      файл, а не отрисовывать в браузере: это персональные данные;
    * ``Cache-Control: no-store`` — выгрузка не должна попасть в кэш
      прокси/браузера (общий запрет кэширования персональных данных).
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Response
from fastapi.encoders import jsonable_encoder
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.models import User
from app.db.session import get_db
from app.modules.auth.deps import get_current_user
from app.modules.privacy.service import build_export, count_user_rows, delete_account_data

__all__ = ["router"]

router = APIRouter(prefix="/account", tags=["privacy"])


@router.get(
    "/export",
    summary="Выгрузка всех персональных данных (152-ФЗ ст. 14, docs/03 §10)",
)
async def export_account(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Полный пакет персональных данных пользователя в формате JSON."""
    payload = await build_export(db, user)
    return Response(
        content=json.dumps(jsonable_encoder(payload), ensure_ascii=False, indent=2),
        media_type="application/json",
        headers={
            # Выгрузка = персональные данные: её нельзя кэшировать.
            "Cache-Control": "no-store",
            "Content-Disposition": 'attachment; filename="career-assistant-data-export.json"',
        },
    )


@router.delete(
    "",
    summary="Каскадное удаление аккаунта и всех данных (152-ФЗ ст. 21, docs/03 §10)",
)
async def delete_account(
    response: Response,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Удалить аккаунт со всеми персональными данными безвозвратно.

    Возвращает отчёт по количеству удалённых строк в каждой таблице —
    это подтверждение исполнения запроса (152-ФЗ ст. 21), поэтому отчёт
    не содержит идентификатора пользователя.
    """
    report = await delete_account_data(db, user)
    # refresh-cookie больше не описывает существующую сессию — удаляем её,
    # иначе браузер продолжит слать мёртвый токен до истечения срока.
    response.delete_cookie(
        key=settings.refresh_cookie_name,
        path=settings.refresh_cookie_path,
        secure=settings.secure_cookies,
        httponly=True,
    )
    response.headers["Cache-Control"] = "no-store"
    return {
        "deleted": True,
        "report": report,
        "message": (
            "Аккаунт и все персональные данные удалены. Восстановление "
            "невозможно; при необходимости воспользуйтесь правом на "
            "регистрацию заново."
        ),
    }


@router.get(
    "/summary",
    summary="Сводка данных аккаунта (что будет удалено)",
)
async def account_summary(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Количество связанных записей — для подтверждения перед удалением."""
    return {"user_id": str(user.id), "email": user.email, **await count_user_rows(db, user.id)}
