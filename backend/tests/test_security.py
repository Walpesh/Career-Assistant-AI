"""Security regression tests (TASK: security hardening).

Покрытие:
    - fail-fast конфигурации: production с дефолтным/коротким JWT_SECRET падает;
    - запрет CORS wildcard '*' вместе с credentials;
    - SQL echo выключён вне debug/production (нет утечки DSN в логи);
    - security-заголовки (CSP, X-Frame-Options, Referrer-Policy);
    - Redis sliding-window rate limiter: правила и ответ 429 (+Retry-After);
    - ротация refresh-токена и обнаружение reuse (revoke all);
    - одноразовые WS-тикеты;
    - отключение /docs, /openapi.json, /redoc в production.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core import redis_client as redis_module
from app.core.config import Settings, settings
from app.core.rate_limit import (
    RATE_LIMIT_RULES,
    SlidingWindowRateLimiter,
    match_rule,
)
from app.db.models import RefreshToken
from app.main import create_app

API = "/api/v1"
PASSWORD = "strongpassword"


# ============================================================
# Конфигурация и секреты (fail-fast)
# ============================================================


def test_production_default_jwt_secret_fails_fast():
    with pytest.raises(ValueError):
        Settings(
            environment="production",
            jwt_secret="change-me-in-production",
            queue_embedded_workers=False,
            _env_file=None,
        )


def test_production_short_jwt_secret_fails_fast():
    with pytest.raises(ValueError):
        Settings(
            environment="production",
            jwt_secret="a" * 31,
            queue_embedded_workers=False,
            _env_file=None,
        )


def test_production_strong_jwt_secret_is_accepted():
    s = Settings(
        environment="production",
        jwt_secret="a" * 40,
        queue_embedded_workers=False,
        _env_file=None,
    )
    assert s.is_production is True


def test_production_embedded_workers_fails_fast():
    """Production требует QUEUE_EMBEDDED_WORKERS=false (worker-изоляция)."""
    with pytest.raises(ValueError, match="QUEUE_EMBEDDED_WORKERS=false"):
        Settings(
            environment="production",
            jwt_secret="a" * 40,
            queue_embedded_workers=True,
            _env_file=None,
        )


def test_effective_embedded_workers_false_in_production():
    s = Settings(
        environment="production",
        jwt_secret="a" * 40,
        queue_embedded_workers=False,
        _env_file=None,
    )
    assert s.effective_embedded_workers is False
    dev = Settings(environment="development", _env_file=None)
    assert dev.effective_embedded_workers is True


def test_cors_wildcard_with_credentials_is_rejected():
    with pytest.raises(ValueError):
        Settings(cors_origins="*", cors_allow_credentials=True, _env_file=None)


def test_db_echo_disabled_outside_debug_and_production():
    assert Settings(debug=False, _env_file=None).db_echo is False
    assert Settings(debug=True, environment="development", _env_file=None).db_echo is True
    assert (
        Settings(
            debug=True,
            environment="production",
            jwt_secret="a" * 40,
            queue_embedded_workers=False,
            _env_file=None,
        ).db_echo
        is False
    )


def test_secure_cookies_auto_in_production():
    dev = Settings(_env_file=None)
    prod = Settings(
        environment="production", jwt_secret="a" * 40, queue_embedded_workers=False, _env_file=None
    )
    assert dev.secure_cookies is False
    assert prod.secure_cookies is True


# ============================================================
# Правила лимитера
# ============================================================


def test_rate_limit_rules_match_spec():
    limits = {rule.name: (rule.limit, rule.window_seconds) for rule in RATE_LIMIT_RULES}
    assert limits["auth_login"] == (5, 60)
    assert limits["auth_register"] == (3, 3600)
    assert limits["parsing"] == (10, 3600)
    assert limits["analysis_run"] == (20, 3600)
    assert limits["profile_convert"] == (10, 3600)


def test_match_rule_maps_paths():
    assert match_rule("POST", f"{API}/auth/login").name == "auth_login"
    assert match_rule("POST", f"{API}/auth/register").name == "auth_register"
    assert match_rule("POST", f"{API}/parsing/auto").name == "parsing"
    assert match_rule("POST", f"{API}/analysis/run").name == "analysis_run"
    assert match_rule("POST", f"{API}/profile/convert-resume").name == "profile_convert"
    # GET и нелимитируемые маршруты — без правила.
    assert match_rule("GET", f"{API}/auth/login") is None
    assert match_rule("POST", f"{API}/profile") is None


# ============================================================
# Security headers
# ============================================================


async def test_security_headers_present_on_responses(client):
    response = await client.get("/health")
    assert response.status_code == 200
    headers = response.headers
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["X-Frame-Options"] == "DENY"
    assert headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
    assert "Content-Security-Policy" in headers


# ============================================================
# Sliding-window лимитер (реальный Redis)
# ============================================================


async def test_sliding_window_limiter_blocks_over_limit(redis_client):
    limiter = SlidingWindowRateLimiter(redis_client)
    key = f"test:sw:{uuid.uuid4().hex}"
    try:
        for _ in range(3):
            assert (await limiter.hit(key, 3, 60)).allowed is True
        blocked = await limiter.hit(key, 3, 60)
        assert blocked.allowed is False
        assert blocked.retry_after >= 1
    finally:
        await redis_client.delete(key)


async def _clear_rate_limit_keys(redis_client) -> None:
    keys = await redis_client.keys("ratelimit:*")
    if keys:
        await redis_client.delete(*keys)


async def test_register_rate_limit_returns_429(client, redis_client, monkeypatch):
    """3 регистрации/час; 4-й запрос → 429 с контрактным payload и Retry-After."""
    monkeypatch.setattr(settings, "rate_limit_enabled", True, raising=False)
    redis_module.set_redis_client(redis_client)
    await _clear_rate_limit_keys(redis_client)
    try:
        for i in range(3):
            response = await client.post(
                f"{API}/auth/register",
                json={"email": f"rl{i}@test.dev", "password": PASSWORD},
            )
            assert response.status_code == 201, response.text

        blocked = await client.post(
            f"{API}/auth/register",
            json={"email": "rl3@test.dev", "password": PASSWORD},
        )
        assert blocked.status_code == 429
        assert blocked.json() == {
            "detail": "Rate limit exceeded",
            "error_code": "RATE_LIMITED",
        }
        assert int(blocked.headers["Retry-After"]) >= 1
    finally:
        redis_module.reset_redis_client()
        await _clear_rate_limit_keys(redis_client)


async def test_login_rate_limit_returns_429(client, redis_client, monkeypatch):
    """5 входов/минуту; 6-й запрос → 429 (даже при неверных учётных данных)."""
    monkeypatch.setattr(settings, "rate_limit_enabled", True, raising=False)
    redis_module.set_redis_client(redis_client)
    await _clear_rate_limit_keys(redis_client)
    try:
        for _ in range(5):
            response = await client.post(
                f"{API}/auth/login",
                json={"email": "nobody@test.dev", "password": PASSWORD},
            )
            assert response.status_code == 401  # неверные creds, лимит не достигнут

        blocked = await client.post(
            f"{API}/auth/login",
            json={"email": "nobody@test.dev", "password": PASSWORD},
        )
        assert blocked.status_code == 429
        assert blocked.json()["error_code"] == "RATE_LIMITED"
        assert blocked.headers.get("Retry-After")
    finally:
        redis_module.reset_redis_client()
        await _clear_rate_limit_keys(redis_client)


# ============================================================
# Refresh-токены: ротация и обнаружение reuse
# ============================================================


async def _register_and_login(client, email: str) -> dict:
    """Регистрация + подтверждение email → пара JWT (docs/03 §2)."""
    from conftest import register_verified

    return await register_verified(client, email, PASSWORD)


async def test_refresh_sets_httponly_cookie_and_rotates(client):
    """login кладёт refresh в cookie; refresh ротирует токен."""
    login = await _register_and_login(client, "rot@test.dev")
    first_refresh = login["refresh_token"]

    # Cookie выставлена (HttpOnly — из JS недоступна).
    assert client.cookies.get(settings.refresh_cookie_name)

    rotated = await client.post(f"{API}/auth/refresh")
    assert rotated.status_code == 200, rotated.text
    assert rotated.json()["access_token"]
    assert rotated.json()["refresh_token"] != first_refresh


async def test_refresh_reuse_detection_revokes_all(client, engine):
    """Повторное использование ротированного токена → 401 и revoke all."""
    login = await _register_and_login(client, "reuse@test.dev")
    first_refresh = login["refresh_token"]

    rotated = await client.post(f"{API}/auth/refresh")
    assert rotated.status_code == 200, rotated.text

    # Предъявляем старый (ротированный) refresh-токен из «чистого» клиента без
    # cookie, иначе backend предпочтёт валидную cookie и reuse не сработает.
    from httpx import ASGITransport, AsyncClient

    from app.main import app

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as fresh:
        reused = await fresh.post(f"{API}/auth/refresh", json={"refresh_token": first_refresh})
    assert reused.status_code == 401
    assert reused.json()["error_code"] == "REFRESH_TOKEN_REUSE"

    # Все refresh-токены пользователя отозваны (reuse → revoke all).
    factory = async_sessionmaker(
        bind=engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
    )
    async with factory() as session:
        rows = list((await session.scalars(select(RefreshToken))).all())
    assert rows
    assert all(row.revoked_at is not None for row in rows)


# ============================================================
# Одноразовые WS-тикеты
# ============================================================


async def test_ws_ticket_is_single_use(client, redis_client):
    redis_module.set_redis_client(redis_client)
    try:
        login = await _register_and_login(client, "ws@test.dev")
        headers = {"Authorization": f"Bearer {login['access_token']}"}

        response = await client.post(f"{API}/auth/ws-ticket", headers=headers)
        assert response.status_code == 200, response.text
        ticket = response.json()["ticket"]
        assert response.json()["expires_in"] == settings.ws_ticket_ttl_seconds

        from app.modules.auth.ws_tickets import consume_ws_ticket

        assert await consume_ws_ticket(ticket)          # первый раз — user_id
        assert await consume_ws_ticket(ticket) is None  # повторно — уже погашен
    finally:
        redis_module.reset_redis_client()


# ============================================================
# Отключение docs/openapi в production
# ============================================================


def test_docs_disabled_in_production(monkeypatch):
    monkeypatch.setattr(settings, "environment", "production", raising=False)
    app = create_app()
    assert app.openapi_url is None
    assert app.docs_url is None
    assert app.redoc_url is None
