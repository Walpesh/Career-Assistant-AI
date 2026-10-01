"""Auth Module — FastAPI-зависимости: аутентификация по Bearer JWT (docs/03 §2).

`get_current_user` — единственная точка проверки access-токена для всех
защищённых эндпоинтов (`/auth/me`, `/profile`, … в docs/03 — «Auth: Да»).
"""

from __future__ import annotations

import uuid

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.db.models import User
from app.db.session import get_db
from app.modules.auth.security import TOKEN_TYPE_ACCESS, TokenError, decode_token

__all__ = ["bearer_scheme", "get_current_user"]

bearer_scheme = HTTPBearer(auto_error=False)


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    db: AsyncSession = Depends(get_db),
) -> User:
    """Извлекает и валидирует access-токен, возвращает активного пользователя.

    Ошибки (docs/03 §1: { detail, error_code }):
        401 UNAUTHORIZED  — нет заголовка / невалидный / просроченный токен;
        403 ACCOUNT_DISABLED — аккаунт деактивирован (docs/02 §3.1 is_active).
    """
    if credentials is None or not credentials.credentials:
        raise AppError(401, "Не авторизован: отсутствует заголовок Authorization", "UNAUTHORIZED")

    try:
        payload = decode_token(credentials.credentials, expected_type=TOKEN_TYPE_ACCESS)
    except TokenError as exc:
        raise AppError(401, str(exc), "INVALID_TOKEN") from exc

    try:
        user_id = uuid.UUID(payload["sub"])
    except (ValueError, TypeError) as exc:
        raise AppError(401, "Некорректный идентификатор пользователя в токене", "INVALID_TOKEN") from exc

    user = await db.scalar(select(User).where(User.id == user_id))
    if user is None:
        raise AppError(401, "Пользователь не найден", "USER_NOT_FOUND")
    if not user.is_active:
        raise AppError(403, "Аккаунт деактивирован", "ACCOUNT_DISABLED")
    return user
