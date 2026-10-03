"""Healthchecks: liveness/readiness для production (Kubernetes probes).

- /health/live  — процесс жив (без внешних зависимостей);
- /health/ready — Postgres (SELECT 1), Redis (PING), Ollama (коннект).
"""

from __future__ import annotations

from typing import Any

from app.core.config import settings

__all__ = ["check_readiness", "check_postgres", "check_redis", "check_ollama"]


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
            return {"status": "up", "http_status": response.status_code}
        return {"status": "down", "error": f"ollama http {response.status_code}"}
    except Exception as exc:  # noqa: BLE001 — readiness не должен падать
        return {"status": "down", "error": str(exc)[:300]}


async def check_readiness() -> dict[str, Any]:
    """Сводный отчёт readiness: status ready|not_ready + per-check детали."""
    postgres = await check_postgres()
    redis = await check_redis()
    ollama = await check_ollama()
    checks = {"postgres": postgres, "redis": redis, "ollama": ollama}
    ready = all(check.get("status") == "up" for check in checks.values())
    return {
        "status": "ready" if ready else "not_ready",
        "app": settings.app_name,
        "version": settings.app_version,
        "environment": settings.environment,
        "checks": checks,
    }
