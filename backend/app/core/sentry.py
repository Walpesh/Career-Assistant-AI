"""Sentry: отслеживание исключений со скраббингом PII.

Правила (docs/01_ARCHITECTURE.md §9):
    - ``sentry_sdk`` и непустой ``SENTRY_DSN`` — единственное условие включения;
      иначе все функции здесь — no-op (разработка/CI работают без Sentry);
    - ``before_send`` вычищает из события Authorization/Cookie/``jwt_secret``,
      email/телефоны/ФИО/текст резюме, WS-тикеты и query-параметры с токенами;
    - ``send_default_pii=False`` + ``request_bodies="never"`` — чтобы PII
      вообще не уходила из SDK;
    - пароли, JWT-токены и raw cookies не логируются и не отправляются.
"""

from __future__ import annotations

from typing import Any

from app.core.logging import (
    MASKED,
    get_request_id,
    sanitize_headers,
    sanitize_payload,
)

__all__ = [
    "capture_exception",
    "capture_message",
    "init_sentry",
    "is_enabled",
    "scrub_event",
]

_enabled = False

#: Параметры query-строки, которые вырезаются из URL события Sentry.
_SENSITIVE_QUERY_KEYS: frozenset[str] = frozenset(
    {"token", "access_token", "refresh_token", "ticket", "ws_ticket", "code"}
)


def is_enabled() -> bool:
    """Инициализирован ли Sentry (DSN задан и SDK доступен)."""
    return _enabled


def _scrub_url(url: Any) -> Any:
    """Убрать из URL query-параметры с токенами (``?token=...`` → MASKED)."""
    if not isinstance(url, str) or "?" not in url:
        return url
    base, _, query = url.partition("?")
    parts = []
    for chunk in query.split("&"):
        key, sep, _ = chunk.partition("=")
        parts.append(f"{key}={MASKED}" if sep and key.lower() in _SENSITIVE_QUERY_KEYS else chunk)
    return f"{base}?{'&'.join(parts)}"


def scrub_event(event: dict[str, Any], _hint: Any = None) -> dict[str, Any]:
    """``before_send``-хук: вычистить PII из события Sentry."""
    try:
        request = event.get("request")
        if isinstance(request, dict):
            headers = request.get("headers")
            if isinstance(headers, dict):
                request["headers"] = sanitize_headers(headers)
            if request.get("cookies") is not None:
                request["cookies"] = MASKED
            if request.get("data") is not None:
                request["data"] = sanitize_payload(request["data"])
            if request.get("query_string") is not None:
                request["query_string"] = MASKED
            if request.get("url") is not None:
                request["url"] = _scrub_url(request["url"])

        for field in ("contexts", "extra", "tags", "exception", "modules"):
            value = event.get(field)
            if isinstance(value, dict):
                event[field] = sanitize_payload(value)

        breadcrumbs = event.get("breadcrumbs")
        if isinstance(breadcrumbs, dict) and isinstance(breadcrumbs.get("values"), list):
            for crumb in breadcrumbs["values"]:
                if not isinstance(crumb, dict):
                    continue
                if isinstance(crumb.get("data"), dict):
                    crumb["data"] = sanitize_payload(crumb["data"])
                if isinstance(crumb.get("message"), str):
                    crumb["message"] = sanitize_payload(crumb["message"])

        user = event.get("user")
        if isinstance(user, dict):
            # Только технический id: email/ip/username — PII, удаляем.
            event["user"] = {"id": user.get("id") or MASKED}
    except Exception:  # noqa: BLE001 — скраббинг не должен ломать отправку
        pass
    return event


def init_sentry(
    *,
    dsn: str = "",
    environment: str = "development",
    release: str = "",
    traces_sample_rate: float = 0.0,
) -> bool:
    """Инициализировать Sentry. ``False`` — если DSN пуст или SDK отсутствует."""
    global _enabled
    dsn = (dsn or "").strip()
    if not dsn:
        _enabled = False
        return False
    try:
        import sentry_sdk
    except Exception:  # noqa: BLE001 — SDK опционален
        _enabled = False
        return False

    integrations: list[Any] = []
    for module_path in (
        "sentry_sdk.integrations.starlette.StarletteIntegration",
        "sentry_sdk.integrations.fastapi.FastApiIntegration",
        "sentry_sdk.integrations.logging.LoggingIntegration",
    ):
        try:
            module_name, _, class_name = module_path.rpartition(".")
            module = __import__(module_name, fromlist=[class_name])
            integrations.append(getattr(module, class_name)())
        except Exception:  # noqa: BLE001 — интеграция опциональна
            continue

    # Options go as a plain dict: the SDK signature differs between sentry-sdk
    # releases (e.g. request_bodies is missing in older type stubs).
    init_options: dict[str, Any] = {
        "dsn": dsn,
        "environment": environment,
        "release": release or None,
        "integrations": integrations,
        "before_send": scrub_event,
        "send_default_pii": False,
        "request_bodies": "never",
        "traces_sample_rate": float(traces_sample_rate or 0.0),
        "attach_stacktrace": True,
    }
    try:
        sentry_sdk.init(**init_options)
    except Exception:  # noqa: BLE001 — неверный DSN не должен ронять старт
        _enabled = False
        return False
    _enabled = True
    return True


def capture_exception(exc: BaseException) -> None:
    """Отправить исключение в Sentry (no-op, если Sentry выключен)."""
    if not _enabled:
        return
    try:
        import sentry_sdk

        with sentry_sdk.new_scope() as scope:
            request_id = get_request_id()
            if request_id:
                scope.set_tag("request_id", request_id)
            sentry_sdk.capture_exception(exc)
    except Exception:  # noqa: BLE001 — телеметрия не должна ломать поток
        pass


def capture_message(message: str, *, level: str = "info") -> None:
    """Отправить сообщение в Sentry (no-op, если Sentry выключен)."""
    if not _enabled:
        return
    try:
        import sentry_sdk

        with sentry_sdk.new_scope() as scope:
            request_id = get_request_id()
            if request_id:
                scope.set_tag("request_id", request_id)
            sentry_sdk.capture_message(str(message), level=level)  # type: ignore[arg-type]
    except Exception:  # noqa: BLE001 — телеметрия не должна ломать поток
        pass
