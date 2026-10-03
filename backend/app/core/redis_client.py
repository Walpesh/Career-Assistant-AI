"""Общий ленивый Redis-клиент приложения (rate limiting, WS-тикеты и пр.).

Отдельный от ARQ-пула клиент: используется для вспомогательных нужд
(скользящее окно лимитов, одноразовые WebSocket-тикеты), которым не нужен
интерфейс очереди задач. Подключение ленивое; если Redis недоступен, функция
возвращает None — вызывающий код решает, деградировать (fail-open) или нет.
"""

from __future__ import annotations

import asyncio
import logging

from app.core.config import settings

logger = logging.getLogger(__name__)

__all__ = ["get_redis_client", "set_redis_client", "reset_redis_client"]

_client = None
_lock = asyncio.Lock()


async def get_redis_client():
    """Вернуть общий Redis-клиент или None, если Redis недоступен."""
    global _client
    if _client is not None:
        return _client
    async with _lock:
        if _client is None:
            try:
                import redis.asyncio as aioredis

                client = aioredis.from_url(settings.redis_url, decode_responses=True)
                await client.ping()
            except Exception:  # noqa: BLE001 — внешний сервис
                logger.warning("Redis клиент недоступен (%s)", settings.redis_url)
                return None
            _client = client
    return _client


def set_redis_client(client) -> None:  # noqa: ANN001 — тестовая подмена
    """Явно задать клиент (используется в тестах)."""
    global _client
    _client = client


def reset_redis_client() -> None:
    """Сбросить кэшированный клиент (тесты/shutdown)."""
    global _client
    _client = None
