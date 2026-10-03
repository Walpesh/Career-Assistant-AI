"""Auth Module — одноразовые тикеты для подключения к WebSocket (docs/03 §8).

Раньше клиент передавал в query-параметре сырой access-JWT. Это оставляло токен
в логах/истории/Referer. Теперь браузер сначала запрашивает короткоживущий
одноразовый тикет (`POST /auth/ws-ticket`), а сам тикет живёт в Redis и
удаляется при первом использовании (GETDEL), поэтому повторно его не применить.
"""

from __future__ import annotations

import uuid

from app.core.config import settings
from app.core.redis_client import get_redis_client

__all__ = ["WS_TICKET_PREFIX", "create_ws_ticket", "consume_ws_ticket"]

#: Префикс ключа Redis для тикета.
WS_TICKET_PREFIX = "ws:ticket:"


async def create_ws_ticket(user_id: str) -> str | None:
    """Создать одноразовый тикет для user_id; None, если Redis недоступен."""
    redis = await get_redis_client()
    if redis is None:
        return None
    ticket = uuid.uuid4().hex
    await redis.set(
        f"{WS_TICKET_PREFIX}{ticket}",
        str(user_id),
        ex=settings.ws_ticket_ttl_seconds,
    )
    return ticket


#: Lua-скрипт атомарного «прочитать и удалить» (GETDEL для старых Redis < 6.2).
_GETDEL_LUA = (
    "local v = redis.call('GET', KEYS[1]); "
    "if v then redis.call('DEL', KEYS[1]) end; "
    "return v"
)


async def consume_ws_ticket(ticket: str | None) -> str | None:
    """Погасить тикет и вернуть user_id (None — если тикет неизвестен/истёк)."""
    if not ticket:
        return None
    redis = await get_redis_client()
    if redis is None:
        return None
    key = f"{WS_TICKET_PREFIX}{ticket}"
    try:
        return await redis.getdel(key)
    except Exception:  # noqa: BLE001 — нет GETDEL (Redis < 6.2) → атомарный Lua
        value = await redis.eval(_GETDEL_LUA, 1, key)
        if isinstance(value, (bytes, bytearray)):
            return value.decode("utf-8")
        return value

