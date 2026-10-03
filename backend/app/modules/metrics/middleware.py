"""Middleware: латентность и счётчики HTTP-запросов (docs/01 §9).

Endpoint-лейбл берётся из нормализованного шаблона маршрута
(``/api/v1/tasks/{task_id}``), а не из сырого пути — иначе рост
кардинальности label'ов разорвал бы Prometheus.
"""

from __future__ import annotations

import time
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.routing import Match

from app.core.logging import get_logger, get_request_id
from app.modules.metrics.registry import observe_http_request

__all__ = ["DEFAULT_ENDPOINT", "MetricsMiddleware", "normalize_endpoint"]

logger = get_logger(__name__)

#: Endpoint для маршрутов, шаблон которых не удалось определить.
DEFAULT_ENDPOINT = "unmatched"


def _route_template(request: Request) -> str | None:
    """Найти шаблон маршрута (path) для текущего запроса."""
    app = request.scope.get("app")
    routes = getattr(app, "routes", None) or request.scope.get("routes") or []
    for route in routes:
        match, _child_scope = route.matches(request.scope)
        if match is Match.FULL:
            return getattr(route, "path", None)
    return None


def normalize_endpoint(request: Request) -> str:
    """Шаблон endpoint'а для метрики (без query-строки и параметров)."""
    template = _route_template(request)
    if template:
        return str(template)
    path = request.scope.get("path") or request.url.path
    # Статические файлы frontend не должны плодить лейблы.
    if "." in path.rsplit("/", 1)[-1]:
        return "static"
    return path or DEFAULT_ENDPOINT


class MetricsMiddleware(BaseHTTPMiddleware):
    """Измеряет латентность каждого HTTP-запроса и пишет JSON-лог запроса."""

    async def dispatch(self, request: Request, call_next: Any) -> Any:
        started = time.perf_counter()
        endpoint = normalize_endpoint(request)
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            return response
        finally:
            duration = time.perf_counter() - started
            try:
                observe_http_request(endpoint, request.method, status_code, duration)
                logger.info(
                    "http_request",
                    method=request.method,
                    endpoint=endpoint,
                    status=status_code,
                    duration_ms=round(duration * 1000, 2),
                    request_id=get_request_id(),
                )
            except Exception:  # noqa: BLE001 — метрики не должны ломать ответ
                pass
