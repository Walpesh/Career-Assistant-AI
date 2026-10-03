"""Healthcheck regression tests (TASK: production infrastructure).

- /health/live — процесс жив (без внешних зависимостей);
- /health/ready — Postgres SELECT 1, Redis PING, Ollama коннект;
  200 = ready, 503 = not_ready.
"""

from __future__ import annotations


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
    assert set(body["checks"]) == {"postgres", "redis", "ollama"}


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
