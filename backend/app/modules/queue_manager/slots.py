"""Ограничители параллелизма Queue Manager на Redis (docs/01 §5, docs/04 §6).

Правила очереди, которые обязаны выполняться всегда — даже если воркеров
несколько процессов:

    - парсинг: не более ``MAX_CONCURRENT_PARSERS_PER_USER`` (2) задач на пользователя;
    - LLM: строго ``MAX_CONCURRENT_LLM_WORKERS`` (1) задача на всё приложение,
      поэтому Ollama никогда не вызывается параллельно (docs/05 §1).

Слот — это ключ в Redis, захваченный через ``SET NX EX``. Ключ имеет TTL,
поэтому упавший воркер не оставляет слот занятым навсегда. Если Redis
недоступен (например, в юнит-тестах), используется эквивалентный
внутрипроцессный счётчик — с тем же публичным интерфейсом.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

from app.core.config import settings

__all__ = ["QueueBusy", "QueueSlots", "RedisQueueSlots", "InProcessQueueSlots"]

logger = logging.getLogger(__name__)

#: Lua-скрипт: слот освобождается только его владельцем (по токену).
_RELEASE_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
end
return 0
"""


class QueueBusy(RuntimeError):
    """Свободного слота не нашлось — задача должна вернуться в очередь."""


class RedisQueueSlots:
    """Слоты в Redis — работают одинаково в любом числе процессов."""

    def __init__(self, redis: Any, *, prefix: str = "career:queue:slot") -> None:
        # redis: клиент redis.asyncio / ArqRedis — нужен только set() и eval().
        self._redis = redis
        self._prefix = prefix

    def _key(self, group: str, index: int) -> str:
        return f"{self._prefix}:{group}:{index}"

    async def acquire(self, group: str, limit: int) -> tuple[int, str] | None:
        """Занять один из ``limit`` слотов группы. None — все слоты заняты."""
        token = uuid.uuid4().hex
        ttl = max(1, int(settings.queue_slot_ttl_seconds))
        for index in range(max(1, limit)):
            acquired = await self._redis.set(
                self._key(group, index), token, nx=True, ex=ttl
            )
            if acquired:
                return index, token
        return None

    async def release(self, group: str, slot: int, token: str) -> None:
        await self._redis.eval(_RELEASE_SCRIPT, 1, self._key(group, slot), token)


class InProcessQueueSlots:
    """Слоты внутри процесса — запасной вариант без Redis (тесты, офлайн)."""

    def __init__(self) -> None:
        self._taken: dict[str, dict[int, str]] = {}
        self._lock = asyncio.Lock()

    async def acquire(self, group: str, limit: int) -> tuple[int, str] | None:
        token = uuid.uuid4().hex
        async with self._lock:
            slots = self._taken.setdefault(group, {})
            for index in range(max(1, limit)):
                if index not in slots:
                    slots[index] = token
                    return index, token
        return None

    async def release(self, group: str, slot: int, token: str) -> None:
        async with self._lock:
            slots = self._taken.get(group)
            if slots and slots.get(slot) == token:
                slots.pop(slot, None)

    async def in_use(self, group: str) -> int:
        """Сколько слотов группы занято (используется в тестах)."""
        async with self._lock:
            return len(self._taken.get(group, {}))


def parsing_slot_group(user_id) -> str:
    """Имя группы слотов парсинга для пользователя (docs/04 §1)."""
    return f"parsing:{user_id}"


#: Группа слотов LLM-очереди — одна на всё приложение (docs/04 §6).
LLM_SLOT_GROUP = "llm"
