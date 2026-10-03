"""Сбор gauge-метрик: длины ARQ-очередей, слоты семафора, доступность Ollama.

Собирается по требованию ``GET /metrics`` (быстро, с коротким таймаутом) и
периодически фоновой задачей lifespan. Любая ошибка сбора не влияет на
ответ эндпоинта — метрика просто остаётся на предыдущем значении.
"""

from __future__ import annotations

import asyncio
from typing import Any

from app.core.config import settings
from app.core.logging import get_logger
from app.modules.metrics.alerts import evaluate, record_ollama_status
from app.modules.metrics.registry import (
    set_ollama_up,
    set_queue_length,
    set_semaphore_slots,
)

__all__ = [
    "COLLECT_INTERVAL_SECONDS",
    "collect_metrics",
    "collect_ollama",
    "collect_queue_metrics",
    "collect_semaphore_metrics",
    "start_collector",
    "stop_collector",
]

logger = get_logger(__name__)

#: Период фонового сбора метрик, секунды.
COLLECT_INTERVAL_SECONDS = 15.0

#: Таймаут обращения к Ollama при сборе, секунды.
_OLLAMA_TIMEOUT_SECONDS = 3.0

_task: asyncio.Task | None = None


async def collect_queue_metrics() -> None:
    """Обновить длины очередей ARQ (parsing / llm) в метриках."""
    try:
        from app.modules.queue_manager.queues import (
            LLM_QUEUE,
            PARSING_QUEUE,
            get_pool,
        )
    except Exception:  # noqa: BLE001 — очередь может быть не загружена
        return
    try:
        pool = await get_pool()
        if pool is None:
            set_queue_length("parsing", 0)
            set_queue_length("llm", 0)
            return
        for metric_name, queue in (("parsing", PARSING_QUEUE), ("llm", LLM_QUEUE)):
            try:
                length = await pool.zcard(queue)
                set_queue_length(metric_name, int(length))
            except Exception:  # noqa: BLE001 — ключ может отсутствовать
                set_queue_length(metric_name, 0)
    except Exception:  # noqa: BLE001 — сбор метрик не должен ломать процесс
        logger.debug("collect_queue_metrics failed", exc_info=True)


async def collect_semaphore_metrics() -> None:
    """Обновить количество активных слотов Redis-семафора."""
    try:
        from app.modules.queue_manager.queues import get_pool
    except Exception:  # noqa: BLE001
        return
    try:
        pool = await get_pool()
        if pool is None:
            set_semaphore_slots("llm", 0)
            set_semaphore_slots("parsing", 0)
            return
        for metric_name in ("llm", "parsing"):
            try:
                active = await pool.keys(f"career:queue:slot:{metric_name}:*")
                set_semaphore_slots(metric_name, len(active))
            except Exception:  # noqa: BLE001
                set_semaphore_slots(metric_name, 0)
    except Exception:  # noqa: BLE001
        logger.debug("collect_semaphore_metrics failed", exc_info=True)


async def collect_ollama() -> None:
    """Обновить доступность Ollama и состояние алерта о её недоступности."""
    try:
        import httpx

        base = settings.ollama_base_url.rstrip("/")
        async with httpx.AsyncClient(timeout=_OLLAMA_TIMEOUT_SECONDS) as client:
            response = await client.get(f"{base}/api/tags")
        up = response.status_code < 500
    except Exception:  # noqa: BLE001 — недоступность Ollama это не исключение
        up = False
    set_ollama_up(up)
    record_ollama_status(up)


async def collect_metrics(*, evaluate_alerts: bool = True) -> None:
    """Один полный цикл сбора метрик и проверки порогов алертов."""
    await asyncio.gather(
        collect_queue_metrics(),
        collect_semaphore_metrics(),
        collect_ollama(),
        return_exceptions=True,
    )
    if evaluate_alerts:
        try:
            evaluate()
        except Exception:  # noqa: BLE001
            logger.debug("alert evaluation failed", exc_info=True)


async def _collector_loop() -> None:
    """Фоновая задача: периодический сбор метрик."""
    while True:
        try:
            await asyncio.sleep(COLLECT_INTERVAL_SECONDS)
            await collect_metrics()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — цикл не должен умирать
            logger.debug("metrics collector iteration failed", exc_info=True)


def start_collector() -> Any:
    """Запустить фоновый сборщик метрик (lifespan startup)."""
    global _task
    if _task is None or _task.done():
        _task = asyncio.create_task(_collector_loop())
    return _task


async def stop_collector() -> None:
    """Остановить фоновый сборщик метрик (lifespan shutdown)."""
    global _task
    if _task is not None and not _task.done():
        _task.cancel()
        try:
            await _task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
    _task = None
