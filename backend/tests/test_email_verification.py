"""Интеграционные тесты: 6-digit email OTP verification при регистрации.

Покрытие (ТЗ «Email OTP Code Verification System»):
    - register создаёт НЕверифицированный аккаунт и не выдаёт JWT;
    - login до подтверждения → 403 EMAIL_NOT_VERIFIED;
    - корректный OTP активирует аккаунт и возвращает пару JWT;
    - неверный / просроченный код → 400 (OTP_INVALID / OTP_EXPIRED);
    - 5 неверных попыток → блокировка (OTP_LOCKED), верный код тоже отклоняется;
    - resend-code: 1 запрос / 60 сек на email (429 + Retry-After),
      после паузы новый код приходит, старый перестаёт работать.

SMTP в тестах конвейера выключен: ``send_verification_email`` перехватывается
автофикстурой conftest (``OTP_OUTBOX``), подтверждение идёт через реальный
POST /auth/verify-email. Отдельный блок ниже восстанавливает настоящий
почтовый слой и подменяет только транспорт ``aiosmtplib.send`` — так
проверяются реальные параметры отправки, отказы SMTP и поведение в
production (503 SMTP_UNAVAILABLE вместо ложного «код отправлен»).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from app.core import mail as mail_module
from app.core.config import settings
from app.db.models import EmailOtp, User
from conftest import OTP_OUTBOX
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

API = "/api/v1"
EMAIL = "otp-user@example.com"
PASSWORD = "strongpassword"
WRONG_CODE = "000000"

#: Настоящая отправка писем, захваченная при импорте модуля — автофикстура
#: conftest (``_capture_otp``) подменяет её уже во время выполнения теста.
#: Нужна тестам самого почтового слоя: иначе SMTP проверять нечем.
_REAL_SEND = mail_module.send_verification_email


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


async def test_register_duplicate_unverified_email_reissues_code(client, factory):
    """Повторная регистрация НЕподтверждённого email → 201 + новый OTP.

    Аккаунт не пересоздаётся (email уникален — вставка дубля упала бы на
    индексе), но пароль обновляется и выдаётся свежий код: владелец мог
    не получить первое письмо. Старый код при этом перестаёт работать.
    """
    await register(client)
    old_code = OTP_OUTBOX[EMAIL]

    response = await client.post(
        f"{API}/auth/register", json={"email": EMAIL.upper(), "password": "newpassword123"}
    )
    assert response.status_code == 201, response.text
    assert response.json() == {
        "message": "Verification code sent to email",
        "email": EMAIL,
    }

    # Новый код выдан, старый более не действует; аккаунт один и не подтверждён.
    new_code = OTP_OUTBOX[EMAIL]
    assert new_code != old_code
    async with factory() as session:
        users = (
            await session.scalars(select(User).where(User.email == EMAIL))
        ).all()
        assert len(users) == 1
        assert users[0].is_verified is False

    assert (await verify(client, EMAIL, old_code)).json()["error_code"] == "OTP_INVALID"

    # Новый пароль принят только после подтверждения нового кода.
    await verify(client, EMAIL, new_code)
    assert (await login(client, password="newpassword123")).status_code == 200
    assert (await login(client, password=PASSWORD)).status_code == 401


async def test_register_duplicate_verified_email_still_409(client):
    """Повторная регистрация подтверждённого email → 409 EMAIL_TAKEN.

    Смена пароля чужого подтверждённого аккаунта означала бы его захват,
    поэтому здесь конфликт обязателен (docs/03 §2).
    """
    await register(client)
    assert (await verify(client, EMAIL, OTP_OUTBOX[EMAIL])).status_code == 200

    response = await client.post(
        f"{API}/auth/register", json={"email": EMAIL.upper(), "password": "newpassword123"}
    )
    assert response.status_code == 409
    assert response.json()["error_code"] == "EMAIL_TAKEN"

    # Старый пароль продолжает работать: аккаунт не переписан.
    assert (await login(client, password=PASSWORD)).status_code == 200


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


# ============================================================
# Реальная отправка: aiosmtplib.send замокан, реальный почтовый слой
# ============================================================


@pytest.fixture
def real_smtp(monkeypatch):
    """Настоящий ``send_verification_email`` + настроенный SMTP.

    Автофикстура conftest подменяет отправку целиком (код попадает в
    ``OTP_OUTBOX``), поэтому здесь она восстанавливается: проверяется именно
    почтовый слой — аргументы ``aiosmtplib.send`` и поведение при отказах.
    """
    monkeypatch.setattr(mail_module, "send_verification_email", _REAL_SEND)
    monkeypatch.setattr(settings, "smtp_host", "smtp.example.com")
    monkeypatch.setattr(settings, "smtp_port", 587)
    monkeypatch.setattr(settings, "smtp_user", "mailer@example.com")
    monkeypatch.setattr(settings, "smtp_password", "app-password")
    monkeypatch.setattr(settings, "smtp_security", "starttls")
    monkeypatch.setattr(
        settings, "emails_from", "Career-Assistant-AI <mailer@example.com>"
    )
    return settings


@pytest.fixture
def sent_messages(monkeypatch):
    """Заглушка транспорта ``aiosmtplib.send``: письма и их параметры."""
    import aiosmtplib

    calls: list[dict] = []

    async def _fake_send(message, **kwargs):
        calls.append({"message": message, **kwargs})
        return (250, b"OK")

    monkeypatch.setattr(aiosmtplib, "send", _fake_send)
    return calls


async def test_send_verification_email_calls_smtp_with_configured_params(
    real_smtp, sent_messages
):
    """Письмо уходит на SMTP с параметрами из конфигурации.

    Регрессия «регистрация отвечает 201, но письма не приходят»: раньше отказ
    фоновой отправки терялся, и единственным симптомом был «не пришёл код».
    """
    assert await mail_module.send_verification_email(EMAIL, "123456") is True

    assert len(sent_messages) == 1
    call = sent_messages[0]
    assert call["hostname"] == "smtp.example.com"
    assert call["port"] == 587
    assert call["username"] == "mailer@example.com"
    assert call["password"] == "app-password"
    assert call["start_tls"] is True
    assert call["use_tls"] is False
    assert call["timeout"] == mail_module.SMTP_TIMEOUT_SECONDS

    message = call["message"]
    assert message["To"] == EMAIL
    assert message["From"] == real_smtp.emails_from
    assert "123456" in message["Subject"]
    assert "123456" in message.get_body(preferencelist=("plain",)).get_content()


async def test_send_verification_email_uses_ssl_for_port_465(
    real_smtp, sent_messages, monkeypatch
):
    """Порт 465 = неявный TLS: use_tls=True, start_tls=False."""
    monkeypatch.setattr(real_smtp, "smtp_port", 465)

    assert await mail_module.send_verification_email(EMAIL, "654321") is True

    call = sent_messages[0]
    assert call["port"] == 465
    assert call["use_tls"] is True
    assert call["start_tls"] is False


async def test_smtp_connect_kwargs_reconcile_port_and_security(real_smtp, monkeypatch):
    """Рассогласование порта и TLS-режима исправляется и логируется.

    «465 + starttls» — классическая причина тихого сбоя отправки: сервер
    ждёт TLS сразу после соединения, а клиент начинает plaintext-диалог.
    """
    monkeypatch.setattr(real_smtp, "smtp_port", 465)
    monkeypatch.setattr(real_smtp, "smtp_security", "starttls")
    kwargs = mail_module.smtp_connect_kwargs()
    assert kwargs["use_tls"] is True
    assert kwargs["start_tls"] is False

    monkeypatch.setattr(real_smtp, "smtp_port", 587)
    monkeypatch.setattr(real_smtp, "smtp_security", "ssl")
    kwargs = mail_module.smtp_connect_kwargs()
    assert kwargs["start_tls"] is True
    assert kwargs["use_tls"] is False


async def test_send_verification_email_returns_false_on_smtp_error(
    real_smtp, monkeypatch, caplog
):
    """Ошибка SMTP → ``False`` + лог с причиной (а не «успех» и не raise).

    Раньше отправка шла фоновой задачей, поэтому отказ был виден только как
    «код не пришёл» — теперь причина попадает в лог явно.
    """
    import aiosmtplib

    async def _boom(*args, **kwargs):
        raise aiosmtplib.SMTPException("535 authentication failed")

    monkeypatch.setattr(aiosmtplib, "send", _boom)

    with caplog.at_level("ERROR"):
        assert await mail_module.send_verification_email(EMAIL, "111222") is False

    # Причина отказа в логе, но сам OTP-код не утекает.
    assert "SMTPException" in caplog.text
    assert "111222" not in caplog.text


async def test_send_verification_email_without_smtp_returns_false_and_logs_code(
    real_smtp, monkeypatch, caplog
):
    """Без SMTP_HOST отправка невозможна: ``False`` + код в лог для dev."""
    monkeypatch.setattr(settings, "smtp_host", "")

    with caplog.at_level("WARNING"):
        assert await mail_module.send_verification_email(EMAIL, "999888") is False

    assert "999888" in caplog.text  # разработчик видит код и завершает флоу


async def test_send_verification_email_survives_missing_aiosmtplib(
    real_smtp, monkeypatch, caplog
):
    """Нет aiosmtplib → ``False`` с логом, а не ImportError на весь процесс."""
    import builtins

    real_import = builtins.__import__

    def _no_aiosmtplib(name, *args, **kwargs):
        if name == "aiosmtplib":
            raise ImportError("No module named 'aiosmtplib'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _no_aiosmtplib)

    with caplog.at_level("ERROR"):
        assert await mail_module.send_verification_email(EMAIL, "112233") is False

    assert "aiosmtplib" in caplog.text


# ============================================================
# Production: 503 SMTP_UNAVAILABLE вместо ложного «код отправлен»
# ============================================================


@pytest.fixture
def production_env(monkeypatch):
    """``ENVIRONMENT=production`` (остальные проверки Settings уже пройдены)."""
    monkeypatch.setattr(settings, "environment", "production")
    return settings


async def test_register_returns_503_when_smtp_unavailable(
    client, production_env, monkeypatch
):
    """Production + недоставленное письмо → 503 SMTP_UNAVAILABLE, а не 201.

    Главный антифальсификат: раньше register всегда отвечал 201, даже если
    письмо упало, и пользователь бесконечно ждал несуществующий код.
    """
    async def _failed(email: str, code: str, **_: object) -> bool:
        return False

    monkeypatch.setattr(mail_module, "send_verification_email", _failed)

    response = await client.post(
        f"{API}/auth/register", json={"email": EMAIL, "password": PASSWORD}
    )

    assert response.status_code == 503, response.text
    body = response.json()
    assert body["error_code"] == "SMTP_UNAVAILABLE"
    assert body["detail"] == "Email service unavailable"


async def test_register_returns_503_when_smtp_host_missing(
    client, production_env, monkeypatch
):
    """Production без SMTP_HOST → 503 (настоящий почтовый слой, не заглушка)."""
    monkeypatch.setattr(mail_module, "send_verification_email", _REAL_SEND)
    monkeypatch.setattr(settings, "smtp_host", "")

    response = await client.post(
        f"{API}/auth/register", json={"email": EMAIL, "password": PASSWORD}
    )

    assert response.status_code == 503, response.text
    assert response.json()["error_code"] == "SMTP_UNAVAILABLE"


async def test_register_succeeds_in_production_when_smtp_works(
    client, production_env, monkeypatch, sent_messages
):
    """С production-настройками и рабочим SMTP регистрация успешна (201)."""
    monkeypatch.setattr(mail_module, "send_verification_email", _REAL_SEND)
    monkeypatch.setattr(settings, "smtp_host", "smtp.example.com")
    monkeypatch.setattr(settings, "smtp_port", 587)

    body = await register(client)
    assert body["email"] == EMAIL
    assert len(sent_messages) == 1
    assert sent_messages[0]["message"]["To"] == EMAIL


async def test_register_in_development_without_smtp_still_succeeds(
    client, monkeypatch
):
    """Development без SMTP не блокируется: 201, код печатается в лог."""
    monkeypatch.setattr(mail_module, "send_verification_email", _REAL_SEND)
    monkeypatch.setattr(settings, "smtp_host", "")

    response = await client.post(
        f"{API}/auth/register", json={"email": EMAIL, "password": PASSWORD}
    )

    assert response.status_code == 201, response.text
    assert response.json()["email"] == EMAIL


async def test_resend_returns_503_when_smtp_unavailable(
    client, production_env, monkeypatch, factory
):
    """resend-code в production тоже не врёт об успешной отправке."""
    await register(client)  # аккаунт не подтверждён → resend имеет смысл

    # «Отматываем» время последней отправки за пределы resend-интервала.
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

    async def _failed(email: str, code: str, **_: object) -> bool:
        return False

    monkeypatch.setattr(mail_module, "send_verification_email", _failed)

    response = await resend(client)
    assert response.status_code == 503, response.text
    assert response.json()["error_code"] == "SMTP_UNAVAILABLE"


# ============================================================
# Неподтверждённый пользователь: login и путь восстановления доступа
# ============================================================


async def test_unverified_user_login_is_strictly_blocked(client, factory):
    """``is_verified=False`` → 403 EMAIL_NOT_VERIFIED, токены не выдаются."""
    await register(client)

    response = await login(client)
    assert response.status_code == 403
    body = response.json()
    assert body["error_code"] == "EMAIL_NOT_VERIFIED"
    assert "access_token" not in body and "refresh_token" not in body
    assert settings.refresh_cookie_name not in response.cookies

    async with factory() as session:
        row = await session.scalar(select(User).where(User.email == EMAIL))
        assert row.is_verified is False


async def test_unverified_user_can_request_new_code_via_resend(client, factory):
    """Неверифицированный пользователь получает новый OTP через resend."""
    await register(client)
    old_code = OTP_OUTBOX[EMAIL]

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
    new_code = OTP_OUTBOX[EMAIL]
    assert new_code != old_code

    # Новый код подтверждает аккаунт, старый — нет.
    stale = await verify(client, EMAIL, old_code)
    assert stale.json()["error_code"] == "OTP_INVALID"
    assert (await verify(client, EMAIL, new_code)).status_code == 200
