"""Healthcheck regression tests (TASK: production infrastructure).

- /health/live — процесс жив (без внешних зависимостей);
- /health/ready — Postgres SELECT 1, Redis PING, Ollama коннект, SMTP;
  200 = ready, 503 = not_ready.

SMTP в readiness — опциональная проверка: без SMTP_HOST она ``skipped``
(нормальный dev-режим), а в production пустой SMTP_HOST запрещён fail-fast'ом,
поэтому ``skipped`` не маскирует сломанную доставку писем.
"""

from __future__ import annotations

from app.core.config import settings


async def test_health_live_ok(client):
    response = await client.get("/health/live")
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "ok"


async def test_health_ready_reports_checks(client):
    response = await client.get("/health/ready")
    # В тестовом окружении Redis-пул не инициализирован (RecordingPool),
    # поэтому readiness честно отвечает 503 с деталями по каждой проверке.
    assert response.status_code in (200, 503), response.text
    body = response.json()
    assert body["status"] in ("ready", "not_ready")
    assert set(body["checks"]) == {"postgres", "redis", "ollama", "smtp"}


async def test_health_ready_postgres_check_contract(client):
    """Контракт проверки postgres: dict со status up|down (зависит от окружения).

    /health/ready использует глобальный engine (dev-БД из DATABASE_URL),
    а не тестовый engine фикстуры — поэтому up/down зависит от окружения.
    Проверяем только контракт структуры отчёта.
    """
    response = await client.get("/health/ready")
    body = response.json()
    pg = body["checks"]["postgres"]
    assert pg["status"] in ("up", "down")
    if pg["status"] == "down":
        assert "error" in pg


async def test_check_postgres_up_with_test_engine(engine):
    """check_postgres напрямую: up при доступной БД (тестовый engine)."""
    from sqlalchemy import text

    from app.modules.health import checks as health_checks

    orig_engine = health_checks.engine if hasattr(health_checks, "engine") else None
    _ = orig_engine, text  # engine импортируется внутри функции — подменяем модуль
    import app.db.session as session_module

    orig = session_module.engine
    session_module.engine = engine  # type: ignore[assignment]
    try:
        result = await health_checks.check_postgres()
    finally:
        session_module.engine = orig
    assert result["status"] == "up"


# ============================================================
# SMTP: опциональная проверка доступности почтового сервера
# ============================================================


async def test_check_smtp_skipped_when_host_not_configured(monkeypatch):
    """Без SMTP_HOST проверка skipped, а не down (dev без почты — норма)."""
    from app.modules.health import checks as health_checks

    monkeypatch.setattr(settings, "smtp_host", "")
    result = await health_checks.check_smtp()

    assert result["status"] == "skipped"
    assert "SMTP_HOST" in result["reason"]


async def test_check_smtp_up_when_tcp_connects(monkeypatch):
    """Настроенный SMTP + успешное соединение → status up."""
    import asyncio

    from app.modules.health import checks as health_checks

    monkeypatch.setattr(settings, "smtp_host", "smtp.example.com")
    monkeypatch.setattr(settings, "smtp_port", 587)

    class _Writer:
        def close(self) -> None:
            return None

        async def wait_closed(self) -> None:
            return None

    async def _fake_open_connection(host, port):
        return ("220 smtp.example.com", _Writer())

    monkeypatch.setattr(asyncio, "open_connection", _fake_open_connection)

    result = await health_checks.check_smtp()
    assert result == {"status": "up", "host": "smtp.example.com", "port": 587}


async def test_check_smtp_down_when_connection_fails(monkeypatch):
    """Недоступный сервер → status down с причиной, readiness не падает."""
    import asyncio

    from app.modules.health import checks as health_checks

    monkeypatch.setattr(settings, "smtp_host", "smtp.example.com")
    monkeypatch.setattr(settings, "smtp_port", 587)

    async def _boom(host, port):
        raise ConnectionRefusedError("connection refused")

    monkeypatch.setattr(asyncio, "open_connection", _boom)

    result = await health_checks.check_smtp()
    assert result["status"] == "down"
    assert "refused" in result["error"]


async def test_readiness_is_not_degraded_by_skipped_smtp(monkeypatch):
    """``skipped`` не роняет readiness: dev без SMTP остаётся готовым."""
    from app.modules.health import checks as health_checks

    monkeypatch.setattr(settings, "smtp_host", "")

    async def _up():
        return {"status": "up"}

    monkeypatch.setattr(health_checks, "check_postgres", _up)
    monkeypatch.setattr(health_checks, "check_redis", _up)
    monkeypatch.setattr(health_checks, "check_ollama", _up)

    report = await health_checks.check_readiness()
    assert report["checks"]["smtp"]["status"] == "skipped"
    assert report["status"] == "ready"
