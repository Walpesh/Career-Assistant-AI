"""Интеграционные тесты: 6-digit email OTP verification при регистрации.

Покрытие (ТЗ «Email OTP Code Verification System»):
    - register создаёт НЕверифицированный аккаунт и не выдаёт JWT;
    - login до подтверждения → 403 EMAIL_NOT_VERIFIED;
    - корректный OTP активирует аккаунт и возвращает пару JWT;
    - неверный / просроченный код → 400 (OTP_INVALID / OTP_EXPIRED);
    - 5 неверных попыток → блокировка (OTP_LOCKED), верный код тоже отклоняется;
    - resend-code: 1 запрос / 60 сек на email (429 + Retry-After),
      после паузы новый код приходит, старый перестаёт работать.

SMTP в тестах выключен: ``send_verification_email`` перехватывается
session-фикстурой conftest (``OTP_OUTBOX``), подтверждение идёт через
реальный POST /auth/verify-email.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from app.core.config import settings
from app.db.models import EmailOtp, User
from conftest import OTP_OUTBOX
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

API = "/api/v1"
EMAIL = "otp-user@example.com"
PASSWORD = "strongpassword"
WRONG_CODE = "000000"


# ============================================================
# Помощники
# ============================================================


@pytest.fixture
def factory(engine, client):
    """Sessionmaker тестовой БД (зависит от client — ради dependency override)."""
    return async_sessionmaker(
        bind=engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
    )


async def register(client, email: str = EMAIL) -> dict:
    """POST /auth/register → 201 { message, email } (токенов нет)."""
    response = await client.post(
        f"{API}/auth/register", json={"email": email, "password": PASSWORD}
    )
    assert response.status_code == 201, response.text
    return response.json()


async def login(client, email: str = EMAIL, password: str = PASSWORD):
    """POST /auth/login (без assert — негативные сценарии)."""
    return await client.post(f"{API}/auth/login", json={"email": email, "password": password})


async def verify(client, email: str, code: str):
    """POST /auth/verify-email (без assert — негативные сценарии)."""
    return await client.post(f"{API}/auth/verify-email", json={"email": email, "code": code})


async def resend(client, email: str = EMAIL):
    """POST /auth/resend-code."""
    return await client.post(f"{API}/auth/resend-code", json={"email": email})


async def otp_row(factory, email: str = EMAIL) -> EmailOtp | None:
    async with factory() as session:
        return await session.scalar(select(EmailOtp).where(EmailOtp.email == email))


# ============================================================
# Регистрация: неверифицированный аккаунт, без JWT
# ============================================================


async def test_register_creates_unverified_user_without_tokens(client, factory):
    """Register → 201 { message, email }, is_verified=False, login блокирован."""
    body = await register(client)

    # Форма ответа из ТЗ: без access_token/refresh_token.
    assert body == {
        "message": "Verification code sent to email",
        "email": EMAIL,
    }
    assert "access_token" not in body and "refresh_token" not in body

    # Код действительно «отправлен» (перехвачен outbox'ом) и состоит из 6 цифр.
    code = OTP_OUTBOX.get(EMAIL)
    assert code and len(code) == 6 and code.isdigit()

    # В БД: аккаунт активен, но не подтверждён; хранится только хэш кода.
    async with factory() as session:
        user = await session.scalar(select(User).where(User.email == EMAIL))
        assert user is not None
        assert user.is_verified is False
        row = await session.scalar(select(EmailOtp).where(EmailOtp.email == EMAIL))
        assert row is not None
        assert row.otp_code_hash != code
        assert row.attempts_count == 0
        assert row.expires_at > datetime.now(UTC)

    # Без подтверждения JWT не выдаётся.
    response = await login(client)
    assert response.status_code == 403
    assert response.json()["error_code"] == "EMAIL_NOT_VERIFIED"


async def test_register_duplicate_email_still_409(client):
    await register(client)
    response = await client.post(
        f"{API}/auth/register", json={"email": EMAIL.upper(), "password": PASSWORD}
    )
    assert response.status_code == 409
    assert response.json()["error_code"] == "EMAIL_TAKEN"


# ============================================================
# Успешная верификация → пара JWT
# ============================================================


async def test_correct_otp_activates_user_and_returns_jwt(client, factory):
    """Корректный код: is_verified=True, OTP удалён, выдана пара токенов."""
    await register(client)
    code = OTP_OUTBOX[EMAIL]

    response = await verify(client, EMAIL, code)
    assert response.status_code == 200, response.text
    tokens = response.json()
    assert tokens["token_type"] == "bearer"
    assert tokens["access_token"]
    assert tokens["refresh_token"]
    assert tokens["access_token"] != tokens["refresh_token"]

    async with factory() as session:
        user = await session.scalar(select(User).where(User.email == EMAIL))
        assert user.is_verified is True
        # Одноразовый код удалён после успеха.
        row = await session.scalar(select(EmailOtp).where(EmailOtp.email == EMAIL))
        assert row is None

    # Токен работает и login теперь разрешён.
    me = await client.get(
        f"{API}/auth/me",
        headers={"Authorization": f"Bearer {tokens['access_token']}"},
    )
    assert me.status_code == 200
    assert me.json()["email"] == EMAIL
    assert me.json()["is_verified"] is True

    again = await login(client)
    assert again.status_code == 200, again.text

    # Повторное предъявление того же кода — уже нельзя.
    reused = await verify(client, EMAIL, code)
    assert reused.status_code == 400


# ============================================================
# Неверный / просроченный код → 400
# ============================================================


async def test_invalid_otp_returns_400(client, factory):
    await register(client)

    response = await verify(client, EMAIL, WRONG_CODE)
    assert response.status_code == 400
    assert response.json()["error_code"] == "OTP_INVALID"

    # Пользователь остался неверифицированным, попытка засчитана.
    async with factory() as session:
        user = await session.scalar(select(User).where(User.email == EMAIL))
        assert user.is_verified is False
        row = await session.scalar(select(EmailOtp).where(EmailOtp.email == EMAIL))
        assert row.attempts_count == 1


async def test_expired_otp_returns_400(client, factory):
    """Просроченный (TTL 10 минут) код → 400 OTP_EXPIRED."""
    await register(client)

    async with factory() as session:
        await session.execute(
            update(EmailOtp)
            .where(EmailOtp.email == EMAIL)
            .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
        await session.commit()

    response = await verify(client, EMAIL, OTP_OUTBOX[EMAIL])
    assert response.status_code == 400
    assert response.json()["error_code"] == "OTP_EXPIRED"

    # Просроченный код удаляется: повторная попытка — уже OTP_NOT_FOUND.
    again = await verify(client, EMAIL, OTP_OUTBOX[EMAIL])
    assert again.status_code == 400
    assert again.json()["error_code"] == "OTP_NOT_FOUND"

    async with factory() as session:
        user = await session.scalar(select(User).where(User.email == EMAIL))
        assert user.is_verified is False


async def test_unknown_email_verify_returns_400(client):
    response = await verify(client, "nobody@example.com", "123456")
    assert response.status_code == 400
    assert response.json()["error_code"] == "OTP_NOT_FOUND"


async def test_already_verified_email_does_not_issue_tokens_without_code(client):
    """Повторная отправка кода уже верифицированному email не открывает доступ.

    Регрессия: /verify-email нельзя превращать в обход пароля. Зная email
    верифицированного пользователя, злоумышленник не должен получать пару JWT
    ни по произвольному коду, ни по повторному «подтверждению».
    """
    await register(client)
    original_code = OTP_OUTBOX[EMAIL]
    assert (await verify(client, EMAIL, original_code)).status_code == 200

    # resend для подтверждённого email — тоже без письма и без нового кода.
    resent = await resend(client)
    assert resent.status_code == 200
    assert OTP_OUTBOX[EMAIL] == original_code  # код не перевыпускался

    # Произвольный код на уже верифицированный аккаунт → 400, токенов нет.
    response = await verify(client, EMAIL, WRONG_CODE)
    assert response.status_code == 400, response.text
    assert "access_token" not in response.json()

    # И даже ранее использованный (уже consumed) код не выдаёт новую пару.
    reused = await verify(client, EMAIL, original_code)
    assert reused.status_code == 400, reused.text
    assert "access_token" not in reused.json()


# ============================================================
# Блокировка после 5 неверных попыток
# ============================================================


async def test_locks_after_five_invalid_attempts(client, factory):
    await register(client)
    code = OTP_OUTBOX[EMAIL]

    for _ in range(4):
        response = await verify(client, EMAIL, WRONG_CODE)
        assert response.status_code == 400, response.text
        assert response.json()["error_code"] == "OTP_INVALID", response.text

    # Пятая неверная попытка — код блокируется.
    fifth = await verify(client, EMAIL, WRONG_CODE)
    assert fifth.status_code == 400
    assert fifth.json()["error_code"] == "OTP_LOCKED"

    # Теперь не помогает и ВЕРНЫЙ код: лимит исчерпан, нужен resend.
    correct = await verify(client, EMAIL, code)
    assert correct.status_code == 400
    assert correct.json()["error_code"] == "OTP_LOCKED"

    async with factory() as session:
        row = await session.scalar(select(EmailOtp).where(EmailOtp.email == EMAIL))
        assert row.attempts_count == settings.otp_max_attempts
        user = await session.scalar(select(User).where(User.email == EMAIL))
        assert user.is_verified is False


# ============================================================
# Resend: 1 запрос / 60 секунд на email
# ============================================================


async def test_resend_rate_limited_per_email(client):
    """Сразу после регистрации resend → 429 + Retry-After (интервал 60 сек)."""
    await register(client)

    blocked = await resend(client)
    assert blocked.status_code == 429
    assert blocked.json()["error_code"] == "RATE_LIMITED"
    retry_after = int(blocked.headers["Retry-After"])
    assert 1 <= retry_after <= settings.otp_resend_interval_seconds


async def test_resend_issues_fresh_code_after_interval(client, factory):
    """После интервала resend выдаёт новый код; старый перестаёт работать."""
    await register(client)
    old_code = OTP_OUTBOX[EMAIL]

    # «Отматываем» время последней отправки за пределы интервала.
    async with factory() as session:
        await session.execute(
            update(EmailOtp)
            .where(EmailOtp.email == EMAIL)
            .values(
                created_at=datetime.now(UTC)
                - timedelta(seconds=settings.otp_resend_interval_seconds + 5)
            )
        )
        await session.commit()

    response = await resend(client)
    assert response.status_code == 200, response.text
    assert response.json() == {
        "message": "Verification code sent to email",
        "email": EMAIL,
    }

    new_code = OTP_OUTBOX[EMAIL]
    assert new_code != old_code

    # Старый код больше не действует, новый активирует аккаунт.
    stale = await verify(client, EMAIL, old_code)
    assert stale.status_code == 400
    assert stale.json()["error_code"] == "OTP_INVALID"

    confirmed = await verify(client, EMAIL, new_code)
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["access_token"]


async def test_resend_unknown_email_is_enumeration_safe(client):
    """Resend на несуществующий email отвечает как успешный (без отправки)."""
    before = dict(OTP_OUTBOX)
    response = await resend(client, "ghost@example.com")
    assert response.status_code == 200
    assert response.json()["message"] == "Verification code sent to email"
    assert OTP_OUTBOX == before  # письмо не уходило
