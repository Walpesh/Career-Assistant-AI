"""Metrics Module — Prometheus-эндпоинты (docs/01_ARCHITECTURE.md §9).

    GET /metrics         — Prometheus text exposition (основной эндпоинт);
    GET /metrics/alerts  — JSON-отчёт о состоянии порогов алертов;
    GET /metrics/summary — агрегированный JSON-снимок метрик.

Эндпоинт ``/metrics`` не требует аутентификации (как и стандартные
healthz), но собирается только техническая телеметрия: PII кандидата,
токены и cookies в метрики не попадают.
"""

from __future__ import annotations

from fastapi import APIRouter, Response

from app.modules.metrics.alerts import evaluate
from app.modules.metrics.registry import (
    CONTENT_TYPE,
    metrics_snapshot,
    render_metrics,
)

__all__ = ["router"]

router = APIRouter(tags=["metrics"])


@router.get(
    "/metrics",
    summary="Prometheus metrics (text exposition)",
    response_class=Response,
    include_in_schema=False,
)
async def prometheus_metrics() -> Response:
    """Отдать метрики в формате Prometheus text exposition (v0.0.4)."""
    return Response(content=render_metrics(), media_type=CONTENT_TYPE)


@router.get(
    "/metrics/summary",
    summary="Сводка метрик в JSON",
)
async def metrics_summary() -> dict:
    """Агрегированные значения метрик (для дашбордов и быстрой проверки)."""
    return metrics_snapshot()


@router.get(
    "/metrics/alerts",
    summary="Состояние порогов алертов",
)
async def metrics_alerts() -> dict:
    """Проверить пороги и вернуть отчёт: LLM-очередь, капча, Ollama, отказы."""
    return evaluate()
