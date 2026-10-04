"""Rate limiting: HTTP 429 + ``Retry-After`` по контракту (docs/03 §2, §9).

Правила лимитов заданы в ``app.core.rate_limit.RATE_LIMIT_RULES`` и
задеплоены как Redis-sliding-window middleware (docs/01 §5, docs/03 §2):

    POST /auth/login              — 5 / минуту
    POST /auth/register           — 3 / час
    POST /parsing/*               — 10 / час
    POST /analysis/run            — 20 / час
    POST /profile/convert-resume  — 10 / час

Контракт ответа (docs/03 §2, §9):
    429 {"detail": "Rate limit exceeded", "error_code": "RATE_LIMITED"}
    + заголовок ``Retry-After`` (целые секунды, ≥ 1).

Особенности, которые тоже закрепляются тестами:
    - лимит считается по **двум** ключам: IP и (если есть Bearer) user_id —
      и смена IP, и abuse одного аккаунта ограничиваются отдельно;
    - при выключенном ``RATE_LIMIT_ENABLED`` лимитер выключен;
    - при недоступном Redis лимитер работает **fail-open** (docs/03 §2):
      вспомогательный сервис не роняет API.

Тесты с реальным Redis помечаются ``pytest.skip``, если сервер недоступен
(фикстура ``redis_client`` из tests/conftest.py).
"""

from __future__ import annotations

import time
import uuid

import pytest
import pytest_asyncio
from app.core import redis_client as redis_module
from app.core.config import settings
from app.core.rate_limit import (
    RATE_LIMIT_RULES,
    SlidingWindowRateLimiter,
    match_rule,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

API = "/api/v1"
PASSWORD = "strongpassword"


# ============================================================
# Общие помощники
# ============================================================


@pytest_asyncio.fixture
async def limited(client, redis_client, monkeypatch):
    """Клиент с включённым лимитером и подставленным тестовым Redis."""
    monkeypatch.setattr(settings, "rate_limit_enabled", True, raising=False)
    redis_module.set_redis_client(redis_client)
    await _clear(redis_client)
    try:
        yield client
    finally:
        redis_module.reset_redis_client()
        # Ключи чистим и при провале теста — иначе лимит «протечёт» дальше.
        await _clear(redis_client)


async def _clear(redis_client) -> None:
    """Снести все ключи лимитера (окно могло остаться от прошлого теста)."""
    keys = await redis_client.keys("ratelimit:*")
    if keys:
        await redis_client.delete(*keys)


def _rule(name: str):
    return next(rule for rule in RATE_LIMIT_RULES if rule.name == name)


async def _register_and_login(client, email: str | None = None) -> tuple[str, dict]:
    """Регистрация + подтверждение email → (user_id, Authorization-заголовки).

    Токены выдаёт POST /auth/verify-email: до подтверждения email вход
    запрещён (docs/03 §2).
    """
    from conftest import register_verified, user_id_for

    target = email or f"rl{uuid.uuid4().hex[:10]}@test.dev"
    tokens = await register_verified(client, target, PASSWORD)
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}
    return await user_id_for(client, headers), headers


def _as_text(value) -> str:
    """Kluch Redis -> str.

    Fiksatura ``redis_client`` podklyuchaetsya s ``decode_responses=False``
    (v otlichie ot klienta prilozheniya), poetomu klyuchi prihodyat bytes.
    """
    return value.decode() if isinstance(value, bytes) else str(value)


def _assert_429(response, *, rule) -> None:
    """Единая проверка контракта 429 (docs/03 §2)."""
    assert response.status_code == 429
    assert response.json() == {
        "detail": "Rate limit exceeded",
        "error_code": "RATE_LIMITED",
    }
    # Заголовок обязателен: клиент должен знать, когда повторить попытку.
    assert response.headers.get("Retry-After")
    retry_after = int(response.headers["Retry-After"])
    assert 1 <= retry_after <= rule.window_seconds


@pytest.fixture
def factory(engine, client):
    """Sessionmaker тестовой БД (тот же ``engine``, что у фикстуры client)."""
    return async_sessionmaker(
        bind=engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
    )


@pytest.fixture
def limiter_redis(redis_client):
    """Ссылка на Redis тестов для доступа к ключам лимитера."""
    return redis_client


async def _clear_limiter() -> None:
    """Сбросить окна лимитера перед «дозированной» серией запросов.

    Нужно, потому что подготовка (регистрация/вход) сама тратит лимиты
    ``auth_register`` / ``auth_login``, а проверяем мы другой маршрут.
    """
    import app.core.redis_client as module

    client = await module.get_redis_client()
    if client is None:  # pragma: no cover — фикстура redis_client уже пропустила бы
        pytest.skip("Redis недоступен")
    await _clear(client)


async def _seed_vacancy(factory, user_id: str) -> str:
    """Создать вакансию пользователя напрямую в БД (минуя rate-limited API)."""
    from app.db.models import Vacancy

    async with factory() as session:
        vacancy = Vacancy(
            user_id=uuid.UUID(str(user_id)),
            hh_vacancy_id=uuid.uuid4().hex[:10],
            url="https://hh.ru/vacancy/1",
            title="Python разработчик",
            status="raw",
            source="manual",
        )
        session.add(vacancy)
        await session.commit()
        return str(vacancy.id)


async def _make_enterprise(factory, user_id: str) -> None:
    """Grant an enterprise subscription: removes daily quotas (docs/03 11)."""
    from app.db.models import Subscription

    async with factory() as session:
        session.add(
            Subscription(
                user_id=uuid.UUID(str(user_id)), tier="enterprise", status="active"
            )
        )
        await session.commit()


async def _set_resume(client, headers: dict) -> None:
    """Положить непустой resume_text — иначе /convert-resume вернёт 400."""
    response = await client.put(
        f"{API}/profile",
        json={"resume_text": "Python, FastAPI, PostgreSQL, 6 лет опыта."},
        headers=headers,
    )
    assert response.status_code == 200, response.text


# ============================================================
# Правила и их привязка к маршрутам (docs/03 §2)
# ============================================================


def test_rate_limit_rules_match_documented_limits():
    """Лимиты совпадают с зафиксированными в docs/03 §2."""
    limits = {rule.name: (rule.limit, rule.window_seconds) for rule in RATE_LIMIT_RULES}
    assert limits["auth_login"] == (5, 60)
    assert limits["auth_register"] == (3, 3600)
    assert limits["parsing"] == (10, 3600)
    assert limits["analysis_run"] == (20, 3600)
    assert limits["profile_convert"] == (10, 3600)


@pytest.mark.parametrize(
    ("method", "path", "expected"),
    [
        ("POST", f"{API}/auth/login", "auth_login"),
        ("POST", f"{API}/auth/register", "auth_register"),
        ("POST", f"{API}/parsing/auto", "parsing"),
        ("POST", f"{API}/parsing/group", "parsing"),
        ("POST", f"{API}/parsing/manual", "parsing"),
        ("POST", f"{API}/analysis/run", "analysis_run"),
        ("POST", f"{API}/profile/convert-resume", "profile_convert"),
    ],
)
def test_match_rule_maps_protected_routes(method, path, expected):
    """Каждый защищённый маршрут отображается в своё правило."""
    rule = match_rule(method, path)
    assert rule is not None
    assert rule.name == expected


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", f"{API}/auth/login"),  # ограничен только POST
        ("GET", f"{API}/analysis/run"),  # чтение не ограничено
        ("POST", f"{API}/profile"),  # лимита нет
        ("GET", f"{API}/tasks"),  # список задач не ограничен
        ("POST", f"{API}/auth/logout"),  # logout не ограничен
        ("POST", f"{API}/auth/refresh"),  # refresh не ограничен
    ],
)
def test_unprotected_routes_have_no_rule(method, path):
    """Лимиты не навешиваются на маршруты вне контракта (docs/03 §2)."""
    assert match_rule(method, path) is None


# ============================================================
# Скользящее окно (docs/03 §2)
# ============================================================


async def test_sliding_window_allows_exactly_limit_hits(redis_client):
    """Ровно ``limit`` запросов проходят, следующий — блокируется."""
    limiter = SlidingWindowRateLimiter(redis_client)
    key = f"test:sw:{uuid.uuid4().hex}"
    try:
        for index in range(3):
            result = await limiter.hit(key, 3, 60)
            assert result.allowed is True
            assert result.count == index + 1
        blocked = await limiter.hit(key, 3, 60)
        assert blocked.allowed is False
        assert blocked.retry_after >= 1
    finally:
        await redis_client.delete(key)


async def test_sliding_window_expired_entries_free_quota(redis_client):
    """Отметки вне окна выпадают из ZSET — квота освобождается сама.

    Симулируем «старое» окно: сдвигаем все отметки за границу окна вручную,
    чтобы не ждать реальный TTL в тесте.
    """
    limiter = SlidingWindowRateLimiter(redis_client)
    key = f"test:sw:{uuid.uuid4().hex}"
    try:
        for _ in range(2):
            await limiter.hit(key, 2, 1)
        assert (await limiter.hit(key, 2, 1)).allowed is False

        # Всё в ZSET старше окна (1 с) — следующий hit обязан их вычистить.
        old_score = time.time() - 10_000
        members = {member: old_score for member in await redis_client.zrange(key, 0, -1)}
        await redis_client.zadd(key, members)

        assert (await limiter.hit(key, 2, 1)).allowed is True
        assert await redis_client.zcard(key) == 1
    finally:
        await redis_client.delete(key)


async def test_sliding_window_keys_are_independent(redis_client):
    """Разные ключи не влияют друг на друга (IP против user_id)."""
    limiter = SlidingWindowRateLimiter(redis_client)
    prefix = f"test:sw:{uuid.uuid4().hex}"
    try:
        for _ in range(3):
            await limiter.hit(f"{prefix}:ip:1.2.3.4", 3, 60)
        assert (await limiter.hit(f"{prefix}:ip:1.2.3.4", 3, 60)).allowed is False
        # Другой IP и другой scope — не исчерпаны.
        assert (await limiter.hit(f"{prefix}:ip:5.6.7.8", 3, 60)).allowed is True
        assert (await limiter.hit(f"{prefix}:user:abc", 3, 60)).allowed is True
    finally:
        for suffix in ("ip:1.2.3.4", "ip:5.6.7.8", "user:abc"):
            await redis_client.delete(f"{prefix}:{suffix}")


# ============================================================
# HTTP 429 на реальных маршрутах (docs/03 §2, §9)
# ============================================================


async def test_register_rate_limit_returns_429_with_retry_after(limited):
    """3 регистрации/час; 4-я → 429 RATE_LIMITED + Retry-After."""
    rule = _rule("auth_register")
    for index in range(rule.limit):
        response = await limited.post(
            f"{API}/auth/register",
            json={"email": f"rl{index}@test.dev", "password": PASSWORD},
        )
        assert response.status_code == 201, response.text

    blocked = await limited.post(
        f"{API}/auth/register",
        json={"email": "rl-over@test.dev", "password": PASSWORD},
    )
    _assert_429(blocked, rule=rule)
    # Лимит срабатывает ДО бизнес-логики: email даже не проверялся.
    assert "EMAIL_TAKEN" not in blocked.text


async def test_login_rate_limit_returns_429_with_retry_after(limited):
    """5 входов/минуту; 6-й → 429 (даже при верных учётных данных)."""
    from conftest import register_verified

    rule = _rule("auth_login")
    email = f"login{uuid.uuid4().hex[:8]}@test.dev"
    # Подтверждаем email: без него login вернул бы 403 EMAIL_NOT_VERIFIED,
    # и лимит измерялся бы совсем на другом ответе (docs/03 §2).
    await register_verified(limited, email, PASSWORD)
    await _clear_limiter()

    for _ in range(rule.limit):
        response = await limited.post(
            f"{API}/auth/login", json={"email": email, "password": PASSWORD}
        )
        assert response.status_code == 200, response.text

    blocked = await limited.post(
        f"{API}/auth/login", json={"email": email, "password": PASSWORD}
    )
    _assert_429(blocked, rule=rule)


async def test_login_rate_limit_counts_failed_attempts(limited):
    """Неудачные попытки входа тоже тратят лимит (защита от перебора)."""
    rule = _rule("auth_login")
    for _ in range(rule.limit):
        response = await limited.post(
            f"{API}/auth/login",
            json={"email": "nobody@test.dev", "password": PASSWORD},
        )
        assert response.status_code == 401  # лимит не исчерпан

    blocked = await limited.post(
        f"{API}/auth/login", json={"email": "nobody@test.dev", "password": PASSWORD}
    )
    _assert_429(blocked, rule=rule)


async def test_analysis_run_rate_limit_returns_429(limited, queue_pool, factory):
    """20 POST /analysis/run в час; 21-й → 429 (docs/03 §2, §6)."""
    rule = _rule("analysis_run")
    user_id, headers = await _register_and_login(limited)
    vacancy_id = await _seed_vacancy(factory, user_id)
    await _clear_limiter()

    for _ in range(rule.limit):
        response = await limited.post(
            f"{API}/analysis/run",
            json={"vacancy_ids": [vacancy_id], "mode": "analyze"},
            headers=headers,
        )
        assert response.status_code == 200, response.text

    blocked = await limited.post(
        f"{API}/analysis/run",
        json={"vacancy_ids": [vacancy_id], "mode": "analyze"},
        headers=headers,
    )
    _assert_429(blocked, rule=rule)


async def test_parsing_rate_limit_returns_429(limited, queue_pool, factory):
    """10 POST /parsing/* в час; 11-й → 429 (docs/03 §2, §5)."""
    rule = _rule("parsing")
    user_id, headers = await _register_and_login(limited)
    # Daily quota of the FREE tier for `parse` is 5/day (docs/03 11),
    # while the rate limit is 10/hour: without enterprise the daily quota
    # would trigger first and 429 would come from billing, not the limiter.
    await _make_enterprise(factory, user_id)
    await _clear_limiter()

    payload = {"keywords": ["python"], "max_pages": 1}
    for _ in range(rule.limit):
        response = await limited.post(f"{API}/parsing/auto", json=payload, headers=headers)
        assert response.status_code == 200, response.text

    blocked = await limited.post(f"{API}/parsing/auto", json=payload, headers=headers)
    _assert_429(blocked, rule=rule)


async def test_profile_convert_rate_limit_returns_429(limited, queue_pool):
    """10 POST /profile/convert-resume в час; 11-й → 429 (docs/03 §2, §3)."""
    rule = _rule("profile_convert")
    _, headers = await _register_and_login(limited)
    await _set_resume(limited, headers)
    await _clear_limiter()

    for _ in range(rule.limit):
        response = await limited.post(f"{API}/profile/convert-resume", headers=headers)
        assert response.status_code == 202, response.text

    blocked = await limited.post(f"{API}/profile/convert-resume", headers=headers)
    _assert_429(blocked, rule=rule)


# ============================================================
# Ключи IP и user_id (docs/03 §2)
# ============================================================


async def test_user_key_is_counted_separately_from_ip(limited, redis_client, factory):
    """Лимит считается и по IP, и по user_id — два независимых счётчика.

    Для авторизованного запроса в Redis появляется ключ
    ``ratelimit:analysis_run:user:<user_id>`` (docs/03 §2).
    """
    user_id, headers = await _register_and_login(limited)
    vacancy_id = await _seed_vacancy(factory, user_id)
    await _clear_limiter()

    await limited.post(
        f"{API}/analysis/run",
        json={"vacancy_ids": [vacancy_id], "mode": "analyze"},
        headers=headers,
    )

    keys = await redis_client.keys("ratelimit:analysis_run:*")
    decoded = {_as_text(key) for key in keys}
    assert "ratelimit:analysis_run:ip:127.0.0.1" in decoded
    assert f"ratelimit:analysis_run:user:{user_id}" in decoded


async def test_anonymous_request_only_counts_ip(limited, redis_client):
    """Без Bearer-токена считается только IP — user_id неизвестен."""
    rule = _rule("auth_login")
    for _ in range(rule.limit):
        response = await limited.post(
            f"{API}/auth/login",
            json={"email": "nobody@test.dev", "password": PASSWORD},
        )
        assert response.status_code == 401

    keys = await redis_client.keys("ratelimit:auth_login:*")
    decoded = {_as_text(key) for key in keys}
    assert decoded
    assert all(":ip:" in key for key in decoded), decoded


# ============================================================
# Включение/отключение и fail-open (docs/03 §2)
# ============================================================


async def test_limiter_disabled_by_setting(client, redis_client):
    """``RATE_LIMIT_ENABLED=false`` — лимиты не применяются вовсе.

    conftest по умолчанию выключает лимитер, чтобы остальные тесты не
    зависели от Redis; здесь проверяем именно это поведение.
    """
    keys = await redis_client.keys("ratelimit:*")
    if keys:
        await redis_client.delete(*keys)

    for index in range(10):
        response = await client.post(
            f"{API}/auth/login",
            json={"email": f"off{index}@test.dev", "password": PASSWORD},
        )
        assert response.status_code == 401, response.text
    assert await redis_client.keys("ratelimit:*") == []


async def test_limiter_fails_open_when_redis_unavailable(client, monkeypatch):
    """Redis недоступен → запросы проходят (fail-open), API не падает.

    Недоступность вспомогательного сервиса не должна ронять приложение
    (docs/03 §2).
    """
    import app.core.redis_client as module

    from conftest import register_verified

    async def _no_redis():
        return None

    monkeypatch.setattr(module, "get_redis_client", _no_redis)
    monkeypatch.setattr(settings, "rate_limit_enabled", True, raising=False)

    email = f"failopen{uuid.uuid4().hex[:8]}@test.dev"
    tokens = await register_verified(client, email, PASSWORD)

    logged_in = await client.post(
        f"{API}/auth/login", json={"email": email, "password": PASSWORD}
    )
    assert logged_in.status_code == 200, logged_in.text

    headers = {"Authorization": f"Bearer {logged_in.json()['access_token']}"}
    me = await client.get(f"{API}/auth/me", headers=headers)
    assert me.status_code == 200, me.text
    # Токен, выданный подтверждением email, тоже работает (docs/03 §2).
    assert tokens["access_token"]
    assert tokens["access_token"] != logged_in.json()["access_token"]