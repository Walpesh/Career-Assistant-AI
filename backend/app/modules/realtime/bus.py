"""Realtime & Notification Module — доставка событий в WebSocket (docs/03 §8).

Модуль состоит из двух частей:

    router.py — WebSocket-канал и реестр соединений пользователя;
    bus.py    — шина событий на Redis (pub/sub).

Зачем нужна шина: ARQ-воркер может работать отдельным процессом
(`arq app.modules.queue_manager.worker:ParsingQueueSettings`), и тогда он
не разделяет память с FastAPI-процессом, где живут WebSocket-соединения.
Поэтому воркер не обращается к соединениям напрямую, а публикует событие
в Redis-канал, а API-процесс (RealtimeBridge) читает канал и отправляет
событие в сокеты пользователя. Так событие доходит до фронтенда в любом
развёртывании — и при встроенных, и при отдельных воркерах.

Формат сообщения канала (docs/03 §8): {"event": ..., "data": {...}},
сериализуется только через json.dumps — тот же контракт, что у
encode_event() в router.py.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from app.modules.realtime.router import broadcast_to_user

__all__ = ["EVENT_CHANNEL", "publish_event", "RealtimeBridge"]

logger = logging.getLogger(__name__)

#: Redis-канал событий Realtime Module.
EVENT_CHANNEL = "career:ws:events"


async def publish_event(user_id: str, event: str, data: dict[str, Any]) -> bool:
    """Опубликовать событие пользователя в Redis-канал.

    Возвращает True, если событие ушло в Redis. Если Redis недоступен,
    событие доставляется напрямую в локальные WebSocket-соединения —
    это корректно, потому что при недоступном Redis канал никто не читает
    и двойной доставки не будет.
    """
    from app.modules.queue_manager.queues import get_pool

    message = json.dumps(
        {"user_id": user_id, "event": event, "data": data or {}}, ensure_ascii=False
    )
    try:
        pool = await get_pool()
        if pool is not None:
            await pool.publish(EVENT_CHANNEL, message)
            return True
    except Exception:  # noqa: BLE001 — доставка не должна ронять задачу
        logger.warning("Не удалось опубликовать событие %s в Redis", event, exc_info=True)

    await broadcast_to_user(user_id, event, data)
    return False


class RealtimeBridge:
    """Подписка API-процесса на канал событий (lifespan FastAPI)."""

    def __init__(self) -> None:
        self._pubsub: Any = None
        self._task: Any = None

    async def start(self) -> None:
        """Начать читать канал. Ошибки Redis не поднимают приложение."""
        from app.modules.queue_manager.queues import get_pool

        try:
            pool = await get_pool()
            if pool is None:
                logger.warning("Realtime Bridge: Redis-пул недоступен, канал не подключён")
                return
            self._pubsub = pool.pubsub(ignore_subscribe_messages=True)
            await self._pubsub.subscribe(EVENT_CHANNEL)
            self._task = await self._loop(self._pubsub)
            logger.info("Realtime Bridge: подписан на канал %s", EVENT_CHANNEL)
        except Exception:  # noqa: BLE001 — realtime не должен ронять старт
            logger.warning("Realtime Bridge: не удалось подписаться на канал", exc_info=True)

    @staticmethod
    async def _loop(pubsub: Any) -> Any:
        import asyncio

        async def _consume() -> None:
            while True:
                message = await pubsub.get_message(
                    ignore_subscribe_messages=True, timeout=10.0
                )
                if message is None:
                    continue
                await RealtimeBridge._handle(message.get("data"))

        return asyncio.create_task(_consume())

    @staticmethod
    async def _handle(raw: Any) -> None:
        """Разобрать сообщение канала и отправить его в сокеты пользователя."""
        try:
            if isinstance(raw, (bytes, bytearray)):
                raw = raw.decode("utf-8")
            payload = json.loads(raw)
            user_id = str(payload["user_id"])
            event = str(payload["event"])
            data = payload.get("data") or {}
        except Exception:  # noqa: BLE001 — мусор в канале не должен ломать канал
            logger.warning("Realtime Bridge: некорректное сообщение в канале", exc_info=True)
            return

        await broadcast_to_user(user_id, event, data)

    async def stop(self) -> None:
        """Остановить чтение канала и закрыть подписку."""
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except Exception:  # noqa: BLE001 — штатная отмена
                pass
            self._task = None
        if self._pubsub is not None:
            try:
                await self._pubsub.unsubscribe(EVENT_CHANNEL)
                await self._pubsub.aclose()
            except Exception:  # noqa: BLE001 — закрытие не критично
                logger.debug("Realtime Bridge: ошибка закрытия подписки", exc_info=True)
            self._pubsub = None
