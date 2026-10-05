"""Healthchecks: liveness/readiness для production (Kubernetes probes).

- /health/live  — процесс жив (без внешних зависимостей);
- /health/ready — Postgres (SELECT 1), Redis (PING), Ollama (коннект),
                  SMTP (опционально — только если SMTP_HOST задан).
"""

from __future__ import annotations

import asyncio
from typing import Any

from app.core.config import settings

__all__ = [
    "check_readiness",
    "check_postgres",
    "check_redis",
    "check_ollama",
    "check_smtp",
]


def _record_ollama(up: bool) -> None:
    """Обновить метрику доступности Ollama и состояние алерта (docs/01 §9)."""
    try:
        from app.modules.metrics.alerts import record_ollama_status
        from app.modules.metrics.registry import set_ollama_up

        set_ollama_up(up)
        record_ollama_status(up)
    except Exception:  # noqa: BLE001 — метрики не должны ломать readiness
        pass


async def check_postgres() -> dict[str, Any]:
    """Postgres: SELECT 1 через текущий engine."""
    try:
        from sqlalchemy import text

        from app.db.session import engine

        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return {"status": "up"}
    except Exception as exc:  # noqa: BLE001 — readiness не должен падать
        return {"status": "down", "error": str(exc)[:300]}


async def check_redis() -> dict[str, Any]:
    """Redis: PING через общий ARQ-пул (без создания нового при недоступности)."""
    try:
        from app.modules.queue_manager.queues import _pool

        if _pool is None:
            return {"status": "down", "error": "redis pool is not initialized"}
        await _pool.ping()
        return {"status": "up"}
    except Exception as exc:  # noqa: BLE001 — readiness не должен падать
        return {"status": "down", "error": str(exc)[:300]}


async def check_ollama() -> dict[str, Any]:
    """Ollama: коннект к /api/tags (модель может быть не загружена — это не 503)."""
    try:
        import httpx

        base = settings.ollama_base_url.rstrip("/")
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(f"{base}/api/tags")
        if response.status_code < 500:
            _record_ollama(True)
            return {"status": "up", "http_status": response.status_code}
        _record_ollama(False)
        return {"status": "down", "error": f"ollama http {response.status_code}"}
    except Exception as exc:  # noqa: BLE001 — readiness не должен падать
        _record_ollama(False)
        return {"status": "down", "error": str(exc)[:300]}


async def check_smtp() -> dict[str, Any]:
    """SMTP: установка TCP-соединения с почтовым сервером.

    Опциональная проверка (docs/03 §2): если ``SMTP_HOST`` не задан, отдаём
    ``skipped`` — в development это нормальный режим, и «down» здесь означал бы
    ложную деградацию. В production пустой ``SMTP_HOST`` запрещён fail-fast'ом
    в :class:`Settings`, поэтому ``skipped`` там означает лишь отсутствие
    проверки, а не работоспособную отправку.

    Соединение закрывается сразу: проверяется доступность сервера, а не
    отправка письма (проверка не должна слать мусорные OTP-коды и не должна
    логироваться как отправка кода пользователю).
    """
    if not settings.smtp_configured:
        return {"status": "skipped", "reason": "SMTP_HOST is not configured"}

    try:
        from app.core.mail import smtp_connect_kwargs

        kwargs = smtp_connect_kwargs()
        host, port = kwargs["hostname"], kwargs["port"]
        writer = None
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port),
                timeout=5.0,
            )
        finally:
            if writer is not None:
                writer.close()
                await writer.wait_closed()
        return {"status": "up", "host": host, "port": port}
    except Exception as exc:  # noqa: BLE001 — readiness не должен падать
        return {"status": "down", "error": str(exc)[:300]}


async def check_readiness() -> dict[str, Any]:
    """Сводный отчёт readiness: status ready|not_ready + per-check детали.

    ``skipped`` (не настроенный SMTP) не считается деградацией: иначе
    development-окружение без почтового сервера всегда было бы not_ready.
    """
    postgres = await check_postgres()
    redis = await check_redis()
    ollama = await check_ollama()
    smtp = await check_smtp()
    checks = {"postgres": postgres, "redis": redis, "ollama": ollama, "smtp": smtp}
    ready = all(check.get("status") in ("up", "skipped") for check in checks.values())
    return {
        "status": "ready" if ready else "not_ready",
        "app": settings.app_name,
        "version": settings.app_version,
        "environment": settings.environment,
        "checks": checks,
    }
