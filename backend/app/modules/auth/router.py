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

from fastapi import APIRouter, Cookie, Depends, Request, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.errors import AppError
from app.db.models import User, UserProfile
from app.db.session import get_db
from app.modules.auth.deps import get_current_user
from app.modules.auth.schemas import (
    LoginRequest,
    LogoutResponse,
    RefreshRequest,
    RegisterRequest,
    TokenResponse,
    UserOut,
    WsTicketResponse,
)
from app.modules.auth.security import (
    TOKEN_TYPE_REFRESH,
    TokenError,
    create_access_token,
    decode_token,
    hash_password,
    verify_password,
)
from app.modules.auth.tokens import (
    RefreshTokenError,
    RefreshTokenReuseError,
    issue_refresh_token,
    revoke_refresh_token,
    rotate_refresh_token,
)
from app.modules.auth.ws_tickets import create_ws_ticket

router = APIRouter(prefix="/auth", tags=["auth"])


def _user_id(value: str) -> uuid.UUID:
    """UUID из claim `sub`; ошибка → 401 (контрактный формат)."""
    try:
        return uuid.UUID(value)
    except (ValueError, TypeError) as exc:
        raise AppError(
            401, "Некорректный идентификатор пользователя в токене", "INVALID_TOKEN"
        ) from exc


def _set_refresh_cookie(response: Response, token: str) -> None:
    """Положить refresh-токен в HttpOnly; Secure; SameSite=Strict cookie."""
    max_age = settings.refresh_token_expire_days * 24 * 3600
    response.set_cookie(
        key=settings.refresh_cookie_name,
        value=token,
        max_age=max_age,
        httponly=True,
        secure=settings.secure_cookies,
        samesite=settings.refresh_cookie_samesite,
        path=settings.refresh_cookie_path,
    )


def _clear_refresh_cookie(response: Response) -> None:
    """Удалить refresh-cookie (logout)."""
    response.delete_cookie(
        key=settings.refresh_cookie_name,
        path=settings.refresh_cookie_path,
        secure=settings.secure_cookies,
        httponly=True,
        samesite=settings.refresh_cookie_samesite,
    )


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
    summary="Вход (выдаёт access JWT + refresh-cookie)",
)
async def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> TokenResponse:
    """Проверяет пароль и выдаёт пару токенов (docs/03 §2).

    Refresh-токен дополнительно кладётся в HttpOnly; Secure; SameSite=Strict
    cookie и регистрируется в refresh_tokens (ротация + reuse detection).
    """
    user = await db.scalar(select(User).where(User.email == payload.email))
    if user is None or not verify_password(payload.password, user.password_hash):
        raise AppError(
            status.HTTP_401_UNAUTHORIZED, "Неверный email или пароль", "INVALID_CREDENTIALS"
        )
    if not user.is_active:
        raise AppError(status.HTTP_403_FORBIDDEN, "Аккаунт деактивирован", "ACCOUNT_DISABLED")

    refresh_token = await issue_refresh_token(
        db, user, user_agent=request.headers.get("user-agent")
    )
    await db.commit()
    _set_refresh_cookie(response, refresh_token)

    return TokenResponse(
        access_token=create_access_token(user),
        refresh_token=refresh_token,
    )


@router.post(
    "/refresh",
    response_model=TokenResponse,
    summary="Обновление пары токенов (ротация refresh)",
)
async def refresh(
    request: Request,
    response: Response,
    payload: RefreshRequest | None = None,
    refresh_cookie: str | None = Cookie(default=None, alias=settings.refresh_cookie_name),
    db: AsyncSession = Depends(get_db),
) -> TokenResponse:
    """Валидирует refresh-токен (cookie или тело) и ротирует его (docs/03 §2).

    Приоритет — cookie. Повторное предъявление уже ротированного токена
    трактуется как кража: все сессии пользователя отзываются (401 REUSE).
    """
    # Приоритет — явный refresh_token из тела (серверные/legacy-клиенты),
    # иначе берём HttpOnly cookie (браузерный клиент).
    presented = (payload.refresh_token if payload and payload.refresh_token else None) or refresh_cookie
    if not presented:
        raise AppError(
            status.HTTP_401_UNAUTHORIZED,
            "Refresh-токен отсутствует",
            "INVALID_REFRESH_TOKEN",
        )

    try:
        refresh_payload = decode_token(presented, expected_type=TOKEN_TYPE_REFRESH)
    except TokenError as exc:
        raise AppError(status.HTTP_401_UNAUTHORIZED, str(exc), "INVALID_REFRESH_TOKEN") from exc

    user = await db.scalar(select(User).where(User.id == _user_id(refresh_payload["sub"])))
    if user is None:
        raise AppError(status.HTTP_401_UNAUTHORIZED, "Пользователь не найден", "USER_NOT_FOUND")
    if not user.is_active:
        raise AppError(status.HTTP_403_FORBIDDEN, "Аккаунт деактивирован", "ACCOUNT_DISABLED")

    try:
        new_refresh = await rotate_refresh_token(
            db, user, presented, user_agent=request.headers.get("user-agent")
        )
    except RefreshTokenReuseError as exc:
        await db.commit()  # фиксируем отзыв всех токенов
        raise AppError(
            status.HTTP_401_UNAUTHORIZED, str(exc), "REFRESH_TOKEN_REUSE"
        ) from exc
    except RefreshTokenError as exc:
        raise AppError(
            status.HTTP_401_UNAUTHORIZED, str(exc), "INVALID_REFRESH_TOKEN"
        ) from exc

    await db.commit()
    _set_refresh_cookie(response, new_refresh)

    return TokenResponse(
        access_token=create_access_token(user),
        refresh_token=new_refresh,
    )


@router.post("/logout", response_model=LogoutResponse, summary="Выход (отзыв refresh-токена)")
async def logout(
    response: Response,
    refresh_cookie: str | None = Cookie(default=None, alias=settings.refresh_cookie_name),
    db: AsyncSession = Depends(get_db),
) -> LogoutResponse:
    """Отзывает предъявленный refresh-токен и очищает cookie."""
    revoked = False
    if refresh_cookie:
        revoked = await revoke_refresh_token(db, refresh_cookie)
        await db.commit()
    _clear_refresh_cookie(response)
    return LogoutResponse(revoked=revoked)


@router.post(
    "/ws-ticket",
    response_model=WsTicketResponse,
    summary="Одноразовый тикет для WebSocket (docs/03 §8)",
)
async def ws_ticket(user: User = Depends(get_current_user)) -> WsTicketResponse:
    """Выдать короткоживущий одноразовый билет для подключения к /ws."""
    ticket = await create_ws_ticket(str(user.id))
    if ticket is None:
        raise AppError(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Сервис тикетов недоступен, повторите попытку позже",
            "TICKET_UNAVAILABLE",
        )
    return WsTicketResponse(ticket=ticket, expires_in=settings.ws_ticket_ttl_seconds)


@router.get("/me", response_model=UserOut, summary="Текущий пользователь")
async def me(user: User = Depends(get_current_user)) -> User:
    """Данные авторизованного пользователя из access-токена."""
    return user
