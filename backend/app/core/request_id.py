"""Request-ID: уникальный id каждому HTTP-запросу и WebSocket-сессии.

- HTTP (``RequestIDMiddleware``): берёт входящий ``X-Request-ID`` (или генерирует),
  кладёт его в contextvar + ``request.state.request_id`` и возвращает в ответе
  тем же заголовком ``X-Request-ID``;
- WebSocket (``resolve_ws_request_id``): генерирует id соединения из query/headers,
  доступный всем логам realtime-модуля через ``get_request_id()``.

Значения санитизируются (допустимы только ``[A-Za-z0-9_-]``, максимум 64 символа),
чтобы клиент не мог «отравить» логи через заголовок. PII не логируется.
"""

from __future__ import annotations

import re
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from app.core.logging import bind_request_id, new_request_id

__all__ = [
    "REQUEST_ID_HEADER",
    "REQUEST_ID_MAX_LENGTH",
    "RequestIDMiddleware",
    "resolve_ws_request_id",
    "sanitize_request_id",
]

#: Заголовок проброса request_id (входящий и исходящий).
REQUEST_ID_HEADER = "X-Request-ID"

#: Максимальная длина принимаемого клиентом request_id.
REQUEST_ID_MAX_LENGTH = 64

_UNSAFE_CHARS_RE = re.compile(r"[^A-Za-z0-9_-]")


def sanitize_request_id(value: str | None) -> str:
    """Оставить в request_id только безопасные символы, обрезать по длине."""
    cleaned = _UNSAFE_CHARS_RE.sub("", str(value or "").strip())
    return cleaned[:REQUEST_ID_MAX_LENGTH]


class RequestIDMiddleware(BaseHTTPMiddleware):
    """Пробрасывает request_id через contextvar и заголовок ответа."""

    async def dispatch(self, request: Request, call_next: Any) -> Any:
        incoming = sanitize_request_id(request.headers.get(REQUEST_ID_HEADER))
        request_id = bind_request_id(incoming or new_request_id())
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers[REQUEST_ID_HEADER] = request_id
        return response


def resolve_ws_request_id(query_params: Any = None, headers: Any = None) -> str:
    """Request-ID для WebSocket-сессии: из query/headers или новый.

    Значение доступно в логах соединения через ``get_request_id()``.
    """
    candidate = ""
    try:
        if hasattr(query_params, "get"):
            candidate = str(query_params.get("request_id") or query_params.get("requestId") or "")
        if not candidate and hasattr(headers, "get"):
            candidate = str(
                headers.get(REQUEST_ID_HEADER.lower()) or headers.get(REQUEST_ID_HEADER) or ""
            )
    except Exception:  # noqa: BLE001 — query/headers любого типа
        candidate = ""
    return bind_request_id(sanitize_request_id(candidate) or new_request_id())
