"""Углублённая проверка жизненного цикла токенов (docs/03 §2).

Сценарии, которые обязаны оставаться защищёнными после рефакторинга:

1. **Ротация refresh-токена.** ``POST /auth/refresh`` отзывает предъявленный
   токен и выдаёт новый; в БД хранится только SHA-256 хэш (docs/02 §3.11).
2. **Обнаружение повторного использования (reuse detection).** Ротированный
   токен, предъявленный повторно, — признак кражи: **все** сессии
   пользователя отзываются, ответ ``401 REFRESH_TOKEN_REUSE`` (docs/03 §2).
3. **Смена пароля.** Новый пароль работает, старый — нет; при смене пароля
   отзываются все refresh-сессии (иначе украденный токен пережил бы смену).
4. **Мульти-устройства.** Несколько активных refresh-ток coexствуют;
   logout на одном устройстве не убивает остальные, а ``revoke_all_user_tokens``
   закрывает все сразу.
5. **Деактивация аккаунта** блокирует login/refresh выдающим 403
   ``ACCOUNT_DISABLED`` (docs/02 §3.1).

Redis-очередь подменена RecordingPool; LLM и сеть не используются.
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager

import pytest
from app.core.config import settings
from app.db.models import RefreshToken, User
from app.main import app
from app.modules.auth.security import (
    TOKEN_TYPE_ACCESS,
    TOKEN_TYPE_REFRESH,
    TokenError,
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    hash_token,
    verify_password,
)
from app.modules.auth.tokens import (
    RefreshTokenError,
    RefreshTokenReuseError,
    issue_refresh_token,
    revoke_all_user_tokens,
    revoke_refresh_token,
    rotate_refresh_token,
)
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

API = "/api/v1"
PASSWORD = "strongpassword"
NEW_PASSWORD = "brand-new-password"


# ============================================================
# Фикстуры и помощники
# ============================================================


@pytest.fixture
def factory(engine, client):
    """Sessionmaker тестовой БД (зависит от client — ради dependency override)."""
    return async_sessionmaker(
        bind=engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
    )


@asynccontextmanager
async def fresh_client():
    """Отдельный HTTP-клиент: своя cookie-jar = отдельное устройство."""
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as http:
        yield http


async def _register(client, email: str | None = None) -> str:
    """Зарегистрировать + подтвердить email → id пользователя.

    До подтверждения email вход запрещён (docs/03 §2), поэтому токены выдаёт
    verify-email, а не login. Сессия самого подтверждения отзывается: тесты
    этого модуля считают активные refresh-сессии и должны видеть только
    те, что создали через login.
    """
    from conftest import register_verified_without_session

    target = email or f"adv{uuid.uuid4().hex[:10]}@test.dev"
    return await register_verified_without_session(client, target, PASSWORD)


async def _login(client, email: str, password: str = PASSWORD) -> dict:
    response = await client.post(
        f"{API}/auth/login", json={"email": email, "password": password}
    )
    assert response.status_code == 200, response.text
    return response.json()


async def _tokens_of(factory, user_id: str) -> list[RefreshToken]:
    async with factory() as session:
        return list(
            (
                await session.scalars(
                    select(RefreshToken).where(RefreshToken.user_id == uuid.UUID(user_id))
                )
            ).all()
        )


async def _active_tokens_of(factory, user_id: str) -> list[RefreshToken]:
    """Только НЕотозванные токены.

    Реестр хранит историю, а не текущее состояние: ротация и confirm-email
    оставляют отозванные записи. Тесты про активные сессии считают именно их,
    иначе счёт зависел бы от того, была ли уже верификация.
    """
    return [row for row in await _tokens_of(factory, user_id) if row.revoked_at is None]


# ============================================================
# Криптография паролей и JWT (docs/03 §2)
# ============================================================


def test_password_hash_is_bcrypt_and_salted():
    """bcrypt-хэш с индивидуальной солью: два одинаковых пароля → разные хэши."""
    first = hash_password(PASSWORD)
    second = hash_password(PASSWORD)
    assert first != second
    assert first.startswith("$2")
    assert verify_password(PASSWORD, first)
    assert verify_password(PASSWORD, second)
    assert verify_password("wrong-password", first) is False


def test_password_longer_than_72_bytes_is_truncated_not_crashed():
    """bcrypt физически ограничен 72 байтами — длинный пароль обрезается.

    Ошибка была бы 500 на /register; здесь проверяем, что пароль короче
    лимита работает, а сверхлимитный — обрабатывается предсказуемо.
    """
    long_password = "a" * 100
    hashed = hash_password(long_password)
    # Вход с тем же префиксом из 72 байт проходит: пароль обрезан.
    assert verify_password("a" * 72, hashed) is True
    assert verify_password("b" * 72, hashed) is False


def test_verify_password_rejects_malformed_hash():
    """Невалидный хэш не роняет login, а трактуется как неверный пароль."""
    assert verify_password(PASSWORD, "not-a-bcrypt-hash") is False
    assert verify_password(PASSWORD, "") is False


async def test_refresh_token_is_never_stored_in_plaintext(factory, client):
    """В БД лежит только SHA-256 хэш токена, а не сам JWT (docs/02 §3.11)."""
    email = f"hash{uuid.uuid4().hex[:8]}@test.dev"
    user_id = await _register(client, email)
    tokens = await _login(client, email)

    rows = await _tokens_of(factory, user_id)
    assert rows
    # В реестре лежит хэш, а не сам JWT: JWT начинается с «eyJ», хэш — нет,
    # и его длина фиксирована (SHA-256 hex = 64 символа).
    for row in rows:
        assert not row.hashed_token.startswith("eyJ")
        assert len(row.hashed_token) == len(hash_token("x"))
    assert all(tokens["refresh_token"] not in row.hashed_token for row in rows)

    # Активная сессия ровно одна, и её хэш соответствует выданному токену.
    active = await _active_tokens_of(factory, user_id)
    assert [row.hashed_token for row in active] == [hash_token(tokens["refresh_token"])]


def _user_id(tokens: dict) -> str:
    return decode_token(tokens["access_token"], expected_type=TOKEN_TYPE_ACCESS)["sub"]


async def test_access_token_cannot_be_used_as_refresh(client):
    """Тип токена проверяется: access вместо refresh → TokenError (docs/03 §2)."""
    email = f"types{uuid.uuid4().hex[:8]}@test.dev"
    await _register(client, email)
    tokens = await _login(client, email)

    with pytest.raises(TokenError):
        decode_token(tokens["access_token"], expected_type=TOKEN_TYPE_REFRESH)
    with pytest.raises(TokenError):
        decode_token(tokens["refresh_token"], expected_type=TOKEN_TYPE_ACCESS)


async def test_garbage_token_is_rejected(client):
    """Мусор в Bearer → 401 INVALID_TOKEN, не 500."""
    response = await client.get(
        f"{API}/auth/me", headers={"Authorization": "Bearer abc.def.ghi"}
    )
    assert response.status_code == 401
    assert response.json()["error_code"] == "INVALID_TOKEN"


# ============================================================
# Ротация refresh-токена (docs/03 §2)
# ============================================================


async def test_refresh_rotates_token_and_revokes_previous(factory, client):
    """Refresh отзывает предъявленный токен и выдаёт новый."""
    email = f"rot{uuid.uuid4().hex[:8]}@test.dev"
    await _register(client, email)
    first = await _login(client, email)

    rotated = await client.post(f"{API}/auth/refresh")
    assert rotated.status_code == 200, rotated.text
    second = rotated.json()
    assert second["refresh_token"] != first["refresh_token"]
    assert second["access_token"]

    # Ротация: предъявленный токен отозван, активным остался ровно один —
    # новый. В реестре остаётся вся история: сессия подтверждения email
    # (отозвана), сессия login (отозвана ротацией) и новая (активна).
    rows = await _tokens_of(factory, _user_id(first))
    revoked = [row for row in rows if row.revoked_at is not None]
    active = await _active_tokens_of(factory, _user_id(first))
    assert len(rows) == len(revoked) + len(active)
    assert hash_token(first["refresh_token"]) in {row.hashed_token for row in revoked}
    assert [row.hashed_token for row in active] == [hash_token(second["refresh_token"])]


async def test_refresh_cookie_is_httponly_and_path_scoped(client):
    """Refresh хранится в HttpOnly-cookie со scoped path (docs/03 §2).

    JS не должен иметь доступа к cookie — иначе XSS крадёт долгоживущий токен.
    Флаги читаем из заголовка ``Set-Cookie`` ответа, а не из jar httpx.
    """
    email = f"cookie{uuid.uuid4().hex[:8]}@test.dev"
    await _register(client, email)

    response = await client.post(
        f"{API}/auth/login", json={"email": email, "password": PASSWORD}
    )
    assert response.status_code == 200, response.text
    set_cookie = response.headers.get("set-cookie", "")
    lowered = set_cookie.lower()
    assert f"{settings.refresh_cookie_name}=".lower() in lowered
    assert "httponly" in lowered
    assert f"path={settings.refresh_cookie_path}".lower() in lowered
    # SameSite=Strict — cookie не уходит на сторонние запросы (docs/03 §2).
    assert "samesite=strict" in lowered


# ============================================================
# Обнаружение повторного использования (docs/03 §2)
# ============================================================


async def test_refresh_reuse_revokes_all_sessions(factory, client):
    """Повторно предъявленный ротированный токен → revoke all + 401 REUSE.

    Ключевой сценарий защиты от кражи: если старый токен кто-то успел
    сохранить, сервер не может отличить вора от честного клиента, поэтому
    обесценивает **все** сессии пользователя.
    """
    email = f"reuse{uuid.uuid4().hex[:8]}@test.dev"
    user_id = await _register(client, email)
    stolen = await _login(client, email)
    stolen_refresh = stolen["refresh_token"]

    # Ротация: старый токен отозван, выдан новый.
    rotated = await client.post(f"{API}/auth/refresh")
    assert rotated.status_code == 200, rotated.text
    fresh_refresh = rotated.json()["refresh_token"]

    # Второе устройство тоже входит — теперь активны две сессии.
    async with fresh_client() as other_device:
        await other_device.post(f"{API}/auth/login", json={"email": email, "password": PASSWORD})

    # Вор предъявляет украденный (уже ротированный) токен.
    async with fresh_client() as attacker:
        response = await attacker.post(
            f"{API}/auth/refresh", json={"refresh_token": stolen_refresh}
        )
    assert response.status_code == 401
    assert response.json()["error_code"] == "REFRESH_TOKEN_REUSE"

    # Reuse → все сессии пользователя отозваны, включая «честную».
    rows = await _tokens_of(factory, user_id)
    assert rows
    assert all(row.revoked_at is not None for row in rows)

    # Даже актуальный токен честного устройства больше не работает.
    async with fresh_client() as honest:
        blocked = await honest.post(f"{API}/auth/refresh", json={"refresh_token": fresh_refresh})
    assert blocked.status_code == 401


async def test_reuse_detection_can_be_disabled(factory, client, monkeypatch):
    """С выключенным ``REFRESH_TOKEN_REUSE_DETECTION`` reuse → 401, но без revoke all.

    Флаг существует для диагностики; важно, что отключение не превращается
    в возможность войти отозванным токеном.
    """
    monkeypatch.setattr(settings, "refresh_token_reuse_detection", False, raising=False)
    email = f"noreuse{uuid.uuid4().hex[:8]}@test.dev"
    user_id = await _register(client, email)
    first = await _login(client, email)

    rotated = await client.post(f"{API}/auth/refresh")
    assert rotated.status_code == 200

    async with fresh_client() as attacker:
        response = await attacker.post(
            f"{API}/auth/refresh", json={"refresh_token": first["refresh_token"]}
        )
    assert response.status_code == 401
    assert response.json()["error_code"] == "INVALID_REFRESH_TOKEN"

    # Отзыв «всех» не выполнялся — активная сессия сохранилась.
    rows = await _tokens_of(factory, user_id)
    assert any(row.revoked_at is None for row in rows)


async def test_refresh_rejects_unknown_and_malformed_tokens(client):
    """Неизвестный или испорченный refresh → 401 INVALID_REFRESH_TOKEN."""
    async with fresh_client() as device:
        empty = await device.post(f"{API}/auth/refresh")
        assert empty.status_code == 401
        assert empty.json()["error_code"] == "INVALID_REFRESH_TOKEN"

        garbage = await device.post(f"{API}/auth/refresh", json={"refresh_token": "abc.def.ghi"})
        assert garbage.status_code == 401
        assert garbage.json()["error_code"] == "INVALID_REFRESH_TOKEN"


async def test_rotate_unknown_token_raises(factory):
    """Сервисный слой: неизвестный токен → RefreshTokenError, не 500."""
    async with factory() as session:
        user = User(email=f"svc{uuid.uuid4().hex[:8]}@test.dev", password_hash="x")
        session.add(user)
        await session.commit()

        with pytest.raises(RefreshTokenError):
            await rotate_refresh_token(session, user, "не-токен")
        await session.rollback()


# ============================================================
# Смена пароля / сброс пароля (docs/03 §2)
# ============================================================


async def _set_password(factory, user_id: str, new_password: str) -> None:
    """Сменить пароль так, как это делает сервисный слой (bcrypt + revoke all).

    Отдельного эндпоинта сброса пароля в контракте нет (docs/03 §2), поэтому
    проверяется именно набор операций, который обязан выполнять такой
    обработчик: новый bcrypt-хэш и отзыв всех refresh-сессий.
    """
    async with factory() as session:
        user = await session.get(User, uuid.UUID(user_id))
        user.password_hash = hash_password(new_password)
        await session.flush()
        await revoke_all_user_tokens(session, user.id)
        await session.commit()


async def test_password_change_revokes_all_sessions(factory, client):
    """После смены пароля старые refresh-токены не работают (docs/03 §2).

    Иначе украденный токен, выданный до смены пароля, пережил бы её —
    смена пароля перестала бы быть средством восстановления доступа.

    Отозванный токен, предъявленный повторно, попадает в ветку reuse
    detection (401 REFRESH_TOKEN_REUSE) — это ожидаемо: сервер не может
    отличить вора от честного клиента, поэтому обесценивает всё (docs/03 §2).
    """
    email = f"chpwd{uuid.uuid4().hex[:8]}@test.dev"
    user_id = await _register(client, email)
    tokens = await _login(client, email)
    old_refresh = tokens["refresh_token"]

    await _set_password(factory, user_id, NEW_PASSWORD)

    async with fresh_client() as device:
        blocked = await device.post(f"{API}/auth/refresh", json={"refresh_token": old_refresh})
    assert blocked.status_code == 401
    assert blocked.json()["error_code"] in {
        "REFRESH_TOKEN_REUSE",
        "INVALID_REFRESH_TOKEN",
    }

    rows = await _tokens_of(factory, user_id)
    assert rows
    assert all(row.revoked_at is not None for row in rows)


async def test_password_change_blocks_unknown_stolen_token(factory, client):
    """Токен, не зарегистрированный в реестре, отклоняется как неизвестный."""
    email = f"chpwd2{uuid.uuid4().hex[:8]}@test.dev"
    user_id = await _register(client, email)
    await _login(client, email)
    await _set_password(factory, user_id, NEW_PASSWORD)

    async with fresh_client() as device:
        # Подделанный валидный по подписи, но неизвестный серверу токен.
        stolen = await _issue_fake_refresh(factory, user_id)
        blocked = await device.post(f"{API}/auth/refresh", json={"refresh_token": stolen})
    assert blocked.status_code == 401
    assert blocked.json()["error_code"] == "INVALID_REFRESH_TOKEN"


async def _issue_fake_refresh(factory, user_id: str) -> str:
    """Подписать refresh-JWT пользователя, не регистрируя его в реестре."""
    async with factory() as session:
        user = await session.get(User, uuid.UUID(user_id))
        return create_refresh_token(user)


async def test_old_password_stops_working_after_reset(factory, client):
    """После смены пароля старый пароль отвергается, новый принимается."""
    email = f"reset{uuid.uuid4().hex[:8]}@test.dev"
    user_id = await _register(client, email)
    await _set_password(factory, user_id, NEW_PASSWORD)

    async with fresh_client() as device:
        with_old = await device.post(
            f"{API}/auth/login", json={"email": email, "password": PASSWORD}
        )
        assert with_old.status_code == 401
        assert with_old.json()["error_code"] == "INVALID_CREDENTIALS"

        with_new = await device.post(
            f"{API}/auth/login", json={"email": email, "password": NEW_PASSWORD}
        )
        assert with_new.status_code == 200, with_new.text


async def test_password_reset_issues_usable_new_session(factory, client):
    """После сброса пользователь может войти и получить рабочую сессию."""
    email = f"renew{uuid.uuid4().hex[:8]}@test.dev"
    user_id = await _register(client, email)
    await _login(client, email)
    await _set_password(factory, user_id, NEW_PASSWORD)

    async with fresh_client() as device:
        tokens = await _login(device, email, NEW_PASSWORD)
        me = await device.get(
            f"{API}/auth/me", headers={"Authorization": f"Bearer {tokens['access_token']}"}
        )
    assert me.status_code == 200, me.text
    assert me.json()["id"] == user_id


async def test_password_is_never_returned_or_stored_in_plaintext(factory, client):
    """Пароль не возвращается в ответах и не хранится открытым текстом."""
    email = f"plain{uuid.uuid4().hex[:8]}@test.dev"
    user_id = await _register(client, email)
    await _login(client, email)

    async with fresh_client() as device:
        me = await device.get(f"{API}/auth/me")
        profile = await device.get(f"{API}/profile")

    async with factory() as session:
        user = await session.get(User, uuid.UUID(user_id))
        assert PASSWORD not in user.password_hash
        assert user.password_hash.startswith("$2")

    assert PASSWORD not in me.text
    assert PASSWORD not in profile.text
    assert "password" not in me.json()


# ============================================================
# Мульти-устройства и отзыв сессий (docs/03 §2)
# ============================================================


async def test_multiple_devices_have_independent_sessions(factory, client):
    """Три устройства — три активные сессии; logout одного не трогает остальные."""
    email = f"multi{uuid.uuid4().hex[:8]}@test.dev"
    user_id = await _register(client, email)

    # Создаём клиенты напрямую: у asynccontextmanager-генератора нет
    # других держателей, и GC может закрыть его между итерациями.
    # Тот же base_url, что у фикстуры client: TrustedHostMiddleware
    # отклоняет недоверенный Host-заголовок (400 Invalid host header).
    devices = [
        AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver")
        for _ in range(3)
    ]
    try:
        tokens = [await _login(device, email) for device in devices]

        rows = await _tokens_of(factory, user_id)
        assert len([row for row in rows if row.revoked_at is None]) == 3

        # Выход на первом устройстве.
        logout = await devices[0].post(f"{API}/auth/logout")
        assert logout.status_code == 200, logout.text
        assert logout.json()["revoked"] is True

        rows = await _tokens_of(factory, user_id)
        assert len([row for row in rows if row.revoked_at is None]) == 2

        # Остальные устройства продолжают работать.
        for index in (1, 2):
            me = await devices[index].get(
                f"{API}/auth/me", headers={"Authorization": f"Bearer {tokens[index]['access_token']}"}
            )
            assert me.status_code == 200, me.text
    finally:
        for device in devices:
            await device.aclose()


async def test_revoke_all_user_tokens_closes_every_device(factory, client):
    """``revoke_all_user_tokens`` закрывает все устройства разом."""
    email = f"revokeall{uuid.uuid4().hex[:8]}@test.dev"
    user_id = await _register(client, email)
    refreshes = []
    for _ in range(3):
        async with fresh_client() as device:
            refreshes.append((await _login(device, email))["refresh_token"])

    async with factory() as session:
        revoked = await revoke_all_user_tokens(session, uuid.UUID(user_id))
        await session.commit()
    assert revoked == 3

    for refresh in refreshes:
        async with fresh_client() as device:
            response = await device.post(f"{API}/auth/refresh", json={"refresh_token": refresh})
        assert response.status_code == 401


async def test_revoke_all_is_idempotent(factory, client):
    """Повторный revoke-all не падает и не отзывает чужие сессии."""
    email = f"idem{uuid.uuid4().hex[:8]}@test.dev"
    user_id = await _register(client, email)
    async with fresh_client() as device:
        await _login(device, email)

    async with factory() as session:
        user = await session.get(User, uuid.UUID(user_id))
        first = await revoke_all_user_tokens(session, user.id)
        await session.commit()
        second = await revoke_all_user_tokens(session, user.id)
        await session.commit()

    assert first >= 1
    assert second == 0  # активных не осталось


async def test_logout_without_cookie_is_noop(client):
    """Logout без cookie возвращает revoked=false и не падает (docs/03 §2)."""
    response = await client.post(f"{API}/auth/logout")
    assert response.status_code == 200
    assert response.json() == {"revoked": False}


async def test_logout_cookie_is_cleared(client):
    """Logout удаляет refresh-cookie: браузер перестаёт слать мёртвый токен."""
    email = f"logout{uuid.uuid4().hex[:8]}@test.dev"
    await _register(client, email)
    await _login(client, email)

    response = await client.post(f"{API}/auth/logout")
    assert response.status_code == 200
    set_cookie = response.headers.get("set-cookie", "")
    assert f"{settings.refresh_cookie_name}=" in set_cookie
    assert "max-age=0" in set_cookie.lower() or "1970" in set_cookie


# ============================================================
# Деактивация аккаунта (docs/02 §3.1)
# ============================================================


async def test_deactivated_account_cannot_login_or_refresh(factory, client):
    """``is_active = False`` закрывает вход и обновление сессии (403)."""
    email = f"deact{uuid.uuid4().hex[:8]}@test.dev"
    user_id = await _register(client, email)
    tokens = await _login(client, email)

    async with factory() as session:
        user = await session.get(User, uuid.UUID(user_id))
        user.is_active = False
        await session.commit()

    async with fresh_client() as device:
        login = await device.post(
            f"{API}/auth/login", json={"email": email, "password": PASSWORD}
        )
        assert login.status_code == 403
        assert login.json()["error_code"] == "ACCOUNT_DISABLED"

        refresh = await device.post(
            f"{API}/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
        )
        assert refresh.status_code == 403
        assert refresh.json()["error_code"] == "ACCOUNT_DISABLED"


async def test_deactivated_account_access_token_stops_working(factory, client):
    """Ранее выданный access-токен деактивированного аккаунта не работает."""
    email = f"deact2{uuid.uuid4().hex[:8]}@test.dev"
    user_id = await _register(client, email)
    tokens = await _login(client, email)
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}

    async with factory() as session:
        user = await session.get(User, uuid.UUID(user_id))
        user.is_active = False
        await session.commit()

    async with fresh_client() as device:
        response = await device.get(f"{API}/auth/me", headers=headers)
    assert response.status_code == 403
    assert response.json()["error_code"] == "ACCOUNT_DISABLED"


# ============================================================
# Реестр refresh-токенов: сервисный слой (docs/02 §3.11)
# ============================================================


async def test_issue_and_revoke_roundtrip(factory):
    """``issue_refresh_token`` создаёт активную запись, ``revoke_refresh_token`` гасит её."""
    async with factory() as session:
        user = User(email=f"reg{uuid.uuid4().hex[:8]}@test.dev", password_hash="x")
        session.add(user)
        await session.commit()

        token = await issue_refresh_token(session, user, user_agent="pytest")
        await session.commit()

        rows = list((await session.scalars(select(RefreshToken))).all())
        assert len(rows) == 1
        assert rows[0].user_agent == "pytest"
        assert rows[0].revoked_at is None
        assert rows[0].expires_at is not None

        assert await revoke_refresh_token(session, token) is True
        await session.commit()
        # Второй отзыв идемпотентен.
        assert await revoke_refresh_token(session, token) is False


async def test_refresh_token_expiry_is_enforced(factory):
    """Просроченный по expires_at токен отклоняется даже при валидной подписи."""
    from datetime import UTC, datetime, timedelta

    async with factory() as session:
        user = User(email=f"exp{uuid.uuid4().hex[:8]}@test.dev", password_hash="x")
        session.add(user)
        await session.commit()

        token = await issue_refresh_token(session, user)
        await session.commit()

        row = await session.scalar(
            select(RefreshToken).where(RefreshToken.hashed_token == hash_token(token))
        )
        row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await session.commit()

        with pytest.raises(RefreshTokenError):
            await rotate_refresh_token(session, user, token)


async def test_reuse_detection_raises_on_revoked_token(factory):
    """Сервисный слой: предъявление отозванного токена → RefreshTokenReuseError."""
    async with factory() as session:
        user = User(email=f"reuse-svc{uuid.uuid4().hex[:8]}@test.dev", password_hash="x")
        session.add(user)
        await session.commit()

        token = await issue_refresh_token(session, user)
        await session.commit()
        await revoke_refresh_token(session, token)
        await session.commit()

        with pytest.raises(RefreshTokenReuseError):
            await rotate_refresh_token(session, user, token)
        await session.rollback()


async def test_token_helpers_are_consistent(factory):
    """``create_access_token`` содержит sub/type/email; ``hash_token`` — SHA-256."""
    import hashlib

    async with factory() as session:
        user = User(email=f"claims{uuid.uuid4().hex[:8]}@test.dev", password_hash="x")
        session.add(user)
        await session.commit()

        token = create_access_token(user)
        payload = decode_token(token, expected_type=TOKEN_TYPE_ACCESS)
        assert payload["sub"] == str(user.id)
        assert payload["type"] == TOKEN_TYPE_ACCESS
        assert payload["email"] == user.email

    assert hash_token("abc") == hashlib.sha256(b"abc").hexdigest()