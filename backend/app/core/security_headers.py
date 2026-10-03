"""Production security headers middleware (HSTS, CSP, X-Frame-Options и др.).

Добавляет защитные заголовки ко всем ответам. HSTS выставляется только когда
соединение считается защищённым (https или production — за TLS-терминатором),
чтобы не «залипать» на http в локальной разработке.

CSP: политика хранится в settings.csp_policy с плейсхолдером `{nonce}`.
Для HTML-ответов генерируется криптостойкий nonce на запрос, он подставляется
и в заголовок, и в тело ответа вместо плейсхолдера `__CSP_NONCE__`.
Это позволяет обойтись без `'unsafe-inline'` в script-src: единственный
инлайн-скрипт фронтенда (bootstrap в index.html) исполняется только с
совпадающим nonce. Behind nginx тот же nonce берётся из `$request_id`.
"""

from __future__ import annotations

import secrets

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.core.config import settings

__all__ = ["SecurityHeadersMiddleware", "build_csp"]

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

#: Плейсхолдер nonce в разметке фронтенда (frontend/index.html).
_NONCE_PLACEHOLDER = "__CSP_NONCE__"

#: Плейсхолдер nonce в шаблоне политики (settings.csp_policy).
_NONCE_TOKEN = "{nonce}"

#: Content-Type, для которых тело ответа нужно переписать (подставить nonce).
_HTML_CONTENT_TYPE = "text/html"


def build_csp(nonce: str | None = None) -> str:
    """Собрать CSP, подставив nonce вместо плейсхолдера `{nonce}`.

    Без nonce (JSON-ответы API, метрики) источник `nonce-…` убирается целиком,
    и script-src остаётся `'self'` — такие ответы скриптами не исполняются.
    """
    policy = settings.csp_policy
    if not policy:
        return ""
    if nonce is None:
        return policy.replace(f"'nonce-{_NONCE_TOKEN}'", "").replace(_NONCE_TOKEN, "")
    return policy.replace(_NONCE_TOKEN, nonce)


async def _inject_nonce(response, nonce: str):
    """Подставить nonce в тело HTML-ответа вместо `__CSP_NONCE__`.

    Возвращает новый Response с тем же статусом, заголовками и медиатипом.
    Content-Length пересчитывается: после подстановки длина тела меняется,
    иначе клиент получит обрезанный документ.
    """
    chunks: list[bytes] = [chunk async for chunk in response.body_iterator]
    body = b"".join(chunks)
    needle = _NONCE_PLACEHOLDER.encode()
    if needle not in body:
        # Документ без инлайн-скриптов — подстановка не требуется.
        return Response(
            content=body,
            status_code=response.status_code,
            headers=dict(response.headers),
            media_type=response.media_type,
            background=response.background,
        )

    body = body.replace(needle, nonce.encode())
    headers = dict(response.headers)
    # Content-Length больше не действителен; Starlette выставит его сам.
    headers.pop("content-length", None)
    headers.pop("Content-Length", None)
    return Response(
        content=body,
        status_code=response.status_code,
        headers=headers,
        media_type=response.media_type,
        background=response.background,
    )


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Проставляет security-заголовки на каждый HTTP-ответ."""

    async def dispatch(self, request: Request, call_next):
        # Nonce нужен только HTML: он подставляется и в заголовок CSP,
        # и в атрибут nonce инлайн-скрипта внутри тела ответа.
        nonce = secrets.token_urlsafe(16)
        response = await call_next(request)

        content_type = response.headers.get("content-type", "")
        is_html = _HTML_CONTENT_TYPE in content_type
        if is_html:
            response = await _inject_nonce(response, nonce)

        for header, value in _STATIC_HEADERS.items():
            response.headers.setdefault(header, value)

        csp = build_csp(nonce if is_html else None)
        if csp:
            response.headers.setdefault("Content-Security-Policy", csp)

        if settings.is_production or request.url.scheme == "https":
            response.headers.setdefault("Strict-Transport-Security", _HSTS_VALUE)
        return response
