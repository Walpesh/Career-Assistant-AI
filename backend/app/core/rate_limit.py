"""Redis sliding-window rate limiting middleware (security hardening).

Ограничения применяются по двум независимым ключам — IP и (если есть)
аутентифицированный user_id — так что и обход через смену IP, и abuse одного
аккаунта ограничиваются отдельно.

Правила (TASK, security hardening):
    POST /auth/login             — 5 запросов / минуту
    POST /auth/register          — 3 запроса / час
    POST /parsing/*              — 10 запросов / час
    POST /analysis/run           — 20 запросов / час
    POST /profile/convert-resume — 10 запросов / час

Ответ при превышении: HTTP 429
    { "detail": "Rate limit exceeded", "error_code": "RATE_LIMITED" }
    + заголовок Retry-After (секунды).

Если Redis недоступен, лимитер работает fail-open (пропускает запросы):
недоступность вспомогательного сервиса не должна ронять API.
"""

from __future__ import annotations

import math
import time
import uuid
from dataclasses import dataclass

from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from app.core.config import settings
from app.core.redis_client import (
    get_redis_client,
    reset_redis_client,
    set_redis_client,
)

__all__ = [
    "RateLimitRule",
    "RATE_LIMIT_RULES",
    "RateLimitResult",
    "SlidingWindowRateLimiter",
    "get_redis_client",
    "set_redis_client",
    "reset_redis_client",
    "RateLimitMiddleware",
    "match_rule",
]

#: Код ошибки и текст ответа (docs/03 §9 — 429 «Слишком много запросов»).
RATE_LIMITED_CODE = "RATE_LIMITED"
RATE_LIMITED_DETAIL = "Rate limit exceeded"


@dataclass(frozen=True)
class RateLimitRule:
    """Одно правило лимита: метод + путь (точный или префикс)."""

    name: str
    method: str
    path: str
    limit: int
    window_seconds: int
    prefix: bool = False


RATE_LIMIT_RULES: tuple[RateLimitRule, ...] = (
    RateLimitRule("auth_login", "POST", "/auth/login", 5, 60),
    RateLimitRule("auth_register", "POST", "/auth/register", 3, 3600),
    RateLimitRule("parsing", "POST", "/parsing/", 10, 3600, prefix=True),
    RateLimitRule("analysis_run", "POST", "/analysis/run", 20, 3600),
    RateLimitRule("profile_convert", "POST", "/profile/convert-resume", 10, 3600),
)


@dataclass(frozen=True)
class RateLimitResult:
    """Итог проверки одного ключа."""

    allowed: bool
    retry_after: int
    count: int


def _normalize_path(path: str) -> str:
    """Убрать api_prefix, чтобы правила сравнивались с логическим маршрутом."""
    prefix = settings.api_prefix.rstrip("/")
    if prefix and path.startswith(prefix):
        path = path[len(prefix):]
    return path or "/"


def match_rule(method: str, path: str) -> RateLimitRule | None:
    """Подобрать правило для запроса (или None, если лимит не применяется)."""
    normalized = _normalize_path(path)
    for rule in RATE_LIMIT_RULES:
        if rule.method != method.upper():
            continue
        if rule.prefix:
            if normalized.startswith(rule.path):
                return rule
        elif normalized == rule.path or normalized == rule.path.rstrip("/"):
            return rule
    return None


class SlidingWindowRateLimiter:
    """Скользящее окно на Redis sorted set.

    Элементы множества — отметки времени запросов (мс). Элементы старше окна
    удаляются; число оставшихся = число запросов за окно.
    """

    def __init__(self, redis) -> None:  # noqa: ANN001 — redis.asyncio.Redis-like
        self._redis = redis

    async def hit(self, key: str, limit: int, window_seconds: int) -> RateLimitResult:
        now_ms = int(time.time() * 1000)
        window_ms = window_seconds * 1000
        member = f"{now_ms}:{uuid.uuid4().hex}"

        pipe = self._redis.pipeline()
        pipe.zremrangebyscore(key, 0, now_ms - window_ms)
        pipe.zadd(key, {member: now_ms})
        pipe.zcard(key)
        pipe.expire(key, window_seconds + 1)
        results = await pipe.execute()
        count = int(results[2])

        if count <= limit:
            return RateLimitResult(True, 0, count)

        retry_after = window_seconds
        oldest = await self._redis.zrange(key, 0, 0, withscores=True)
        if oldest:
            oldest_ms = float(oldest[0][1])
            retry_after = max(1, math.ceil((oldest_ms + window_ms - now_ms) / 1000))
        return RateLimitResult(False, retry_after, count)


# --- Общий клиент Redis импортируется из app.core.redis_client ---


def _client_ip(request: Request) -> str:
    """IP клиента: X-Forwarded-For (если доверяем прокси) или peer-адрес."""
    if settings.trust_proxy_headers:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _user_id_from_request(request: Request) -> str | None:
    """user_id из Bearer access-токена (best-effort, без обращения к БД)."""
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        return None
    token = header[7:].strip()
    if not token:
        return None
    try:
        from app.modules.auth.security import TOKEN_TYPE_ACCESS, decode_token

        payload = decode_token(token, expected_type=TOKEN_TYPE_ACCESS)
    except Exception:  # noqa: BLE001 — невалидный токен обработает auth-слой
        return None
    return payload.get("sub")


class RateLimitMiddleware(BaseHTTPMiddleware):
    """ASGI-middleware: применяет sliding-window лимиты к защищённым маршрутам."""

    async def dispatch(self, request: Request, call_next):
        if not settings.rate_limit_enabled:
            return await call_next(request)

        rule = match_rule(request.method, request.url.path)
        if rule is None:
            return await call_next(request)

        redis = await get_redis_client()
        if redis is None:
            return await call_next(request)  # fail-open

        limiter = SlidingWindowRateLimiter(redis)
        identifiers: list[tuple[str, str]] = [("ip", _client_ip(request))]
        user_id = _user_id_from_request(request)
        if user_id:
            identifiers.append(("user", user_id))

        for scope, ident in identifiers:
            key = f"ratelimit:{rule.name}:{scope}:{ident}"
            result = await limiter.hit(key, rule.limit, rule.window_seconds)
            if not result.allowed:
                return JSONResponse(
                    status_code=429,
                    content={
                        "detail": RATE_LIMITED_DETAIL,
                        "error_code": RATE_LIMITED_CODE,
                    },
                    headers={"Retry-After": str(result.retry_after)},
                )
        return await call_next(request)
