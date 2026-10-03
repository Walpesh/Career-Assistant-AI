"""Standalone worker entrypoint: отдельный процесс ARQ-воркера.

Production: API и воркеры — независимые рантаймы (docs/01 §6):
  worker-parsing — масштабируется до N реплик;
  worker-llm     — СТРОГО 1 реплика (1-concurrent-worker limit).

Использование (docker-compose.yml services worker-parsing / worker-llm):
  python worker_entry.py parsing   # career:queue:parsing (×N)
  python worker_entry.py llm       # career:queue:llm (строго ×1)

Graceful shutdown: SIGTERM/SIGINT устанавливают флаг остановки
(install_signal_handlers) — ARQ (handle_signals=True) перестаёт брать новые
job'ы, активные завершаются или откладываются (Retry/defer) до остановки пода.
"""

from __future__ import annotations

import asyncio
import logging
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger("worker_entry")


def _usage() -> int:
    print(__doc__, file=sys.stderr)
    print("usage: python worker_entry.py [parsing|llm]", file=sys.stderr)
    return 2


async def _amain(kind: str) -> int:
    from arq import run_worker

    from app.modules.queue_manager.worker import (
        LLMQueueSettings,
        ParsingQueueSettings,
        install_signal_handlers,
    )

    install_signal_handlers()

    if kind == "parsing":
        settings_cls = ParsingQueueSettings
    elif kind == "llm":
        settings_cls = LLMQueueSettings
    else:
        return _usage()

    logger.info(
        "Queue Manager: запуск worker '%s' (queue=%s, max_jobs=%s)",
        kind,
        settings_cls.queue_name,
        settings_cls().max_jobs,
    )
    # Блокирующий запуск ARQ-воркера в отдельном процессе.
    # handle_signals=True в WorkerSettings: SIGTERM/SIGINT — graceful shutdown.
    await run_worker(settings_cls)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        return _usage()
    kind = args[0].strip().lower()
    try:
        return asyncio.run(_amain(kind))
    except KeyboardInterrupt:
        logger.info("Queue Manager: worker остановлен (KeyboardInterrupt)")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
