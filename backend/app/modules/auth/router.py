"""Auth Module — endpoints (docs/03_API_CONTRACTS.md §2).

Контракт:
    POST /auth/register     — регистрация { email, password }
                              → 201 { message, email } (JWT НЕ выдаётся:
                              до подтверждения email аккаунт неверифицирован)
    POST /auth/verify-email  — { email, code } → 200 пара JWT (активация)
    POST /auth/resend-code   — { email } → повторная отправка кода,
                               rate-limit 1/60 сек на email (429 + Retry-After)
    POST /auth/login         — вход; 403 EMAIL_NOT_VERIFIED без подтверждения
    POST /auth/refresh       — обновление пары токенов по refresh-token
    GET  /auth/me            — текущий пользователь (Bearer JWT)

Таблицы: users (docs/02_DATABASE.md §3.1) + создаётся профиль
user_profiles (§3.2) — «Моё резюме» доступно сразу после регистрации;
OTP-коды хранятся в email_otps (10 минут TTL, максимум 5 попыток).

Доставка кода не может «врать» об успехе (docs/03 §2): в development без
SMTP код печатается в лог и регистрация не блокируется, в production любой
отказ отправки — ``503 SMTP_UNAVAILABLE``, потому что «201 + код отправлен»
при недоставленном письме означает, что пользователь бесконечно ждёт
несуществующее письмо.
"""

from __future__ import annotations

import logging
import uuid

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Cookie,
    Depends,
    Request,
    Response,
    status,
)
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import mail
from app.core.config import settings
from app.core.errors import AppError
from app.db.models import User, UserProfile
from app.db.session import get_db
from app.modules.auth.deps import get_current_user
from app.modules.auth.otp import (
    OtpError,
    get_otp,
    issue_otp,
    resend_retry_after,
    verify_otp,
)
from app.modules.auth.schemas import (
    LoginRequest,
    LogoutResponse,
    RefreshRequest,
    RegisterRequest,
    ResendCodeRequest,
    TokenResponse,
    UserOut,
    VerificationSent,
    VerifyEmailRequest,
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

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])

#: error_code отказа почтового сервиса (docs/03 §1 — единый формат ошибок).
SMTP_UNAVAILABLE = "SMTP_UNAVAILABLE"


async def _deliver_code(background_tasks: BackgroundTasks, email: str, code: str) -> bool:
    """Отправить OTP-код и сообщить вызывающему, ушло ли письмо.

    Development: отправка уходит в BackgroundTask (не держит ответ), а если
    SMTP не настроен, код печатается в лог — локальная разработка не встаёт
    колом. Production: отправка синхронная, и её результат определяет ответ
    эндпоинта, иначе «201 Verification code sent» означал бы отправку кода,
    которого пользователь никогда не увидит.

    Returns:
        ``True`` — письмо ушло (или dev-режим без SMTP, где код в логе);
        ``False`` — доставка не удалась и эндпоинт обязан сообщить об этом.

    Raises:
        AppError: 503 SMTP_UNAVAILABLE — production и доставка не удалась.
    """
    if not settings.is_production:
        background_tasks.add_task(mail.send_verification_email, email, code)
        # Код дублируется в лог именно для development без SMTP: без этого
        # разработчик не может завершить регистрацию локально.
        if not settings.smtp_configured:
            logger.warning(
                "SMTP не настроен (development) — OTP-код для %s: %s", email, code
            )
        return True

    delivered = await mail.send_verification_email(email, code)
    if not delivered:
        logger.error(
            "Письмо с OTP-кодом на %s не доставлено — отвечаем 503 вместо успеха",
            email,
        )
        raise AppError(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Email service unavailable",
            SMTP_UNAVAILABLE,
        )
    return True


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
    response_model=VerificationSent,
    status_code=status.HTTP_201_CREATED,
    summary="Регистрация (email + пароль, bcrypt) + отправка OTP-кода",
)
async def register(
    payload: RegisterRequest,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
) -> VerificationSent:
    """Создаёт неверифицированный аккаунт и отправляет 6-значный OTP-код.

    Повторная регистрация **неподтверждённого** email не конфликтует, а
    обновляет пароль и перевыпускает код (владелец мог не получить письмо):
    пересоздать пользователя нельзя — email уникален, и вставка дубля падала
    бы на индексе. 409 EMAIL_TAKEN отдаётся только для **подтверждённого**
    email — там смена пароля означала бы захват чужого аккаунта (docs/03 §9).

    Пара JWT НЕ выдаётся: токены возвращает только POST /auth/verify-email
    после подтверждения email. Если в production письмо не доставилось —
    503 SMTP_UNAVAILABLE, а не ложное «код отправлен».
    """
    existing = await db.scalar(select(User).where(User.email == payload.email))
    if existing is not None:
        if existing.is_verified:
            # Подтверждённый email принадлежит владельцу: не раскрываем, что
            # он зарегистрирован И подтверждён (иначе эндпоинт позволял бы
            # перебирать адреса), но и не позволяем перерегистрацию.
            raise AppError(
                status.HTTP_409_CONFLICT,
                "Пользователь с таким email уже зарегистрирован",
                "EMAIL_TAKEN",
            )
        # Неподтверждённый аккаунт: обновляем пароль и перевыпускаем код.
        # Старый код при этом перестаёт работать (issue_otp удаляет запись),
        # то есть владение email всё равно нужно подтвердить заново.
        existing.password_hash = hash_password(payload.password)
        if await db.scalar(
            select(UserProfile).where(UserProfile.user_id == existing.id)
        ) is None:
            # Подстраховка от «полурегистрации» без профиля (docs/02 §4).
            db.add(UserProfile(user_id=existing.id))
        logger.info("Повторная регистрация неподтверждённого email — выдан новый OTP")
    else:
        user = User(
            email=payload.email,
            password_hash=hash_password(payload.password),
            is_active=True,
            is_verified=False,
        )
        db.add(user)
        await db.flush()  # присваивает user.id (UUID)
        db.add(UserProfile(user_id=user.id))  # профиль 1:1 (docs/02 §4)

    # Код — в базу (хэш), письмо — по результату отправки (см. _deliver_code).
    code = await issue_otp(db, payload.email)
    try:
        await db.commit()
    except IntegrityError as exc:
        # Гонка двух параллельных регистраций одного email: уникальный индекс
        # отработал на коммите. Честный конфликт 409 вместо 500.
        await db.rollback()
        logger.info("Конкурентная регистрация email %s → 409 EMAIL_TAKEN", payload.email)
        raise AppError(
            status.HTTP_409_CONFLICT,
            "Пользователь с таким email уже зарегистрирован",
            "EMAIL_TAKEN",
        ) from exc

    await _deliver_code(background_tasks, payload.email, code)
    return VerificationSent(message="Verification code sent to email", email=payload.email)


@router.post(
    "/verify-email",
    response_model=TokenResponse,
    summary="Подтверждение email 6-значным кодом (активация + пара JWT)",
)
async def verify_email(
    payload: VerifyEmailRequest,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> TokenResponse:
    """Проверяет OTP-код, активирует аккаунт и выдаёт пару токенов.

    Ошибки кода → 400 с error_code (OTP_NOT_FOUND / OTP_EXPIRED /
    OTP_LOCKED / OTP_INVALID).

    Уже подтверждённый email: код всё равно проверяется, а при его отсутствии
    возвращается 400. Раньше здесь был «идемпотентный успех» без проверки кода,
    что превращало эндпоинт в обход пароля: зная email уже верифицированного
    пользователя, можно было получить пару JWT с любыми шестью цифрами.
    """
    user = await db.scalar(select(User).where(User.email == payload.email))
    if user is None:
        raise AppError(
            status.HTTP_400_BAD_REQUEST,
            "Код не запрашивался или уже использован",
            "OTP_NOT_FOUND",
        )

    try:
        await verify_otp(db, payload.email, payload.code)
    except OtpError as exc:
        # Засчитанная попытка (attempts_count) должна сохраниться.
        await db.commit()
        raise AppError(status.HTTP_400_BAD_REQUEST, exc.detail, exc.error_code) from exc

    user.is_verified = True
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
    "/resend-code",
    response_model=VerificationSent,
    summary="Повторная отправка OTP-кода (1 раз в 60 сек на email)",
)
async def resend_code(
    payload: ResendCodeRequest,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
) -> VerificationSent:
    """Генерирует свежий 6-значный код и отправляет письмо.

    Rate-limit: не чаще ``otp_resend_interval_seconds`` (60) с момента
    последней отправки — иначе 429 + ``Retry-After``. Ответ одинаков для
    несуществующего/подтверждённого email (защита от enumeration).

    В production отправка синхронная: неудача — ``503 SMTP_UNAVAILABLE``,
    чтобы «200 + код отправлен» не означал недоставленное письмо.
    """
    user = await db.scalar(select(User).where(User.email == payload.email))
    if user is None or user.is_verified:
        # Не раскрываем, существует ли email: ответ как при успешной отправке.
        return VerificationSent(message="Verification code sent to email", email=payload.email)

    retry_after = resend_retry_after(await get_otp(db, payload.email))
    if retry_after > 0:
        raise AppError(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "Rate limit exceeded",
            "RATE_LIMITED",
            headers={"Retry-After": str(retry_after)},
        )

    code = await issue_otp(db, payload.email)
    await db.commit()

    await _deliver_code(background_tasks, payload.email, code)
    return VerificationSent(message="Verification code sent to email", email=payload.email)


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
    До подтверждения email вход запрещён: 403 EMAIL_NOT_VERIFIED.
    """
    user = await db.scalar(select(User).where(User.email == payload.email))
    if user is None or not verify_password(payload.password, user.password_hash):
        raise AppError(
            status.HTTP_401_UNAUTHORIZED, "Неверный email или пароль", "INVALID_CREDENTIALS"
        )
    if not user.is_active:
        raise AppError(status.HTTP_403_FORBIDDEN, "Аккаунт деактивирован", "ACCOUNT_DISABLED")
    if not user.is_verified:
        raise AppError(
            status.HTTP_403_FORBIDDEN,
            "Email не подтверждён: введите код из письма",
            "EMAIL_NOT_VERIFIED",
        )

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
