"""Production security headers middleware (HSTS, CSP, X-Frame-Options и др.).

Добавляет защитные заголовки ко всем ответам. HSTS выставляется только когда
соединение считается защищённым (https или production — за TLS-терминатором),
чтобы не «залипать» на http в локальной разработке.
"""

from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from app.core.config import settings

__all__ = ["SecurityHeadersMiddleware"]

#: Базовые заголовки безопасности для всех ответов.
_STATIC_HEADERS: dict[str, str] = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Permissions-Policy": "geolocation=(), microphone=(), camera=()",
    "X-XSS-Protection": "0",
}

#: HSTS: год, включая поддомены, с предзагрузкой.
_HSTS_VALUE = "max-age=31536000; includeSubDomains; preload"


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Проставляет security-заголовки на каждый HTTP-ответ."""

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        for header, value in _STATIC_HEADERS.items():
            response.headers.setdefault(header, value)
        if settings.csp_policy:
            response.headers.setdefault("Content-Security-Policy", settings.csp_policy)
        if settings.is_production or request.url.scheme == "https":
            response.headers.setdefault("Strict-Transport-Security", _HSTS_VALUE)
        return response
