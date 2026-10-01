"""Auth Module — endpoints (docs/03_API_CONTRACTS.md §2).

Контракт:
    POST /auth/register — регистрация { email, password }         → 201 UserOut
    POST /auth/login    — вход, выдача JWT { access_token, refresh_token }
    POST /auth/refresh  — обновление пары токенов по refresh-token
    GET  /auth/me       — текущий пользователь (Bearer JWT)

Таблицы: users (docs/02_DATABASE.md §3.1) + создаётся профиль
user_profiles (§3.2) — «Моё резюме» доступно сразу после регистрации.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.db.models import User, UserProfile
from app.db.session import get_db
from app.modules.auth.deps import get_current_user
from app.modules.auth.schemas import (
    LoginRequest,
    RefreshRequest,
    RegisterRequest,
    TokenResponse,
    UserOut,
)
from app.modules.auth.security import (
    TOKEN_TYPE_REFRESH,
    TokenError,
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    verify_password,
)

router = APIRouter(prefix="/auth", tags=["auth"])


def _user_id(value: str) -> uuid.UUID:
    """UUID из claim `sub`; ошибка → 401 (контрактный формат)."""
    try:
        return uuid.UUID(value)
    except (ValueError, TypeError) as exc:
        raise AppError(
            401, "Некорректный идентификатор пользователя в токене", "INVALID_TOKEN"
        ) from exc


@router.post(
    "/register",
    response_model=UserOut,
    status_code=status.HTTP_201_CREATED,
    summary="Регистрация (email + пароль, bcrypt)",
)
async def register(payload: RegisterRequest, db: AsyncSession = Depends(get_db)) -> User:
    """Создаёт аккаунт. 409 — email уже занят (docs/03 §9)."""
    existing = await db.scalar(select(User).where(User.email == payload.email))
    if existing is not None:
        raise AppError(
            status.HTTP_409_CONFLICT,
            "Пользователь с таким email уже зарегистрирован",
            "EMAIL_TAKEN",
        )

    user = User(email=payload.email, password_hash=hash_password(payload.password), is_active=True)
    db.add(user)
    await db.flush()  # присваивает user.id (UUID)
    db.add(UserProfile(user_id=user.id))  # профиль 1:1 (docs/02 §4)
    await db.commit()
    await db.refresh(user)
    return user


@router.post(
    "/login",
    response_model=TokenResponse,
    summary="Вход (выдаёт access + refresh JWT)",
)
async def login(payload: LoginRequest, db: AsyncSession = Depends(get_db)) -> TokenResponse:
    """Проверяет пароль и возвращает пару токенов (docs/03 §2)."""
    user = await db.scalar(select(User).where(User.email == payload.email))
    if user is None or not verify_password(payload.password, user.password_hash):
        raise AppError(
            status.HTTP_401_UNAUTHORIZED, "Неверный email или пароль", "INVALID_CREDENTIALS"
        )
    if not user.is_active:
        raise AppError(status.HTTP_403_FORBIDDEN, "Аккаунт деактивирован", "ACCOUNT_DISABLED")

    return TokenResponse(
        access_token=create_access_token(user),
        refresh_token=create_refresh_token(user),
    )


@router.post(
    "/refresh",
    response_model=TokenResponse,
    summary="Обновление access-токена по refresh-token",
)
async def refresh(payload: RefreshRequest, db: AsyncSession = Depends(get_db)) -> TokenResponse:
    """Валидирует refresh-токен и выдаёт новую пару (ротация)."""
    try:
        refresh_payload = decode_token(payload.refresh_token, expected_type=TOKEN_TYPE_REFRESH)
    except TokenError as exc:
        raise AppError(status.HTTP_401_UNAUTHORIZED, str(exc), "INVALID_REFRESH_TOKEN") from exc

    user = await db.scalar(select(User).where(User.id == _user_id(refresh_payload["sub"])))
    if user is None:
        raise AppError(status.HTTP_401_UNAUTHORIZED, "Пользователь не найден", "USER_NOT_FOUND")
    if not user.is_active:
        raise AppError(status.HTTP_403_FORBIDDEN, "Аккаунт деактивирован", "ACCOUNT_DISABLED")

    return TokenResponse(
        access_token=create_access_token(user),
        refresh_token=create_refresh_token(user),
    )


@router.get("/me", response_model=UserOut, summary="Текущий пользователь")
async def me(user: User = Depends(get_current_user)) -> User:
    """Данные авторизованного пользователя из access-токена."""
    return user
