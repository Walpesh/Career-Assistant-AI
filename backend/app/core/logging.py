"""Structured JSON logging (structlog) with PII sanitization.

Spec: docs/01_ARCHITECTURE.md §9 (Observability).

Features:
    - JSON renderer for production output (structlog + stdlib uvicorn loggers);
    - ``request_id`` bound to every HTTP request and WebSocket session (contextvar);
    - header/payload sanitization: Authorization, Cookie, ``jwt_secret`` and
      candidate personal identifiers (email/phone/name/resume text) never logged.

``structlog`` is an optional dependency: when absent, a stdlib JSON formatter
with the same output shape is used instead.
"""

from __future__ import annotations

import contextvars
import json
import logging
import re
import sys
import uuid
from datetime import UTC, datetime
from typing import Any

__all__ = [
    "MASKED",
    "REQUEST_ID_CTX",
    "SENSITIVE_HEADERS",
    "SENSITIVE_KEYS",
    "bind_request_id",
    "clear_request_id",
    "configure_logging",
    "get_logger",
    "get_request_id",
    "mask_token",
    "new_request_id",
    "sanitize_headers",
    "sanitize_payload",
    "set_request_id",
]

#: Placeholder emitted instead of any secret / personal data.
MASKED = "***MASKED***"

#: Headers fully masked (names are case-insensitive per RFC 7239).
SENSITIVE_HEADERS: frozenset[str] = frozenset(
    {
        "authorization",
        "proxy-authorization",
        "cookie",
        "set-cookie",
        "x-api-key",
        "api-key",
        "x-auth-token",
        "x-csrf-token",
    }
)

#: Payload keys fully masked at any nesting depth.
SENSITIVE_KEYS: frozenset[str] = frozenset(
    {
        "authorization",
        "cookie",
        "cookies",
        "set-cookie",
        "jwt_secret",
        "jwt-secret",
        "access_token",
        "refresh_token",
        "id_token",
        "ws_ticket",
        "token",
        "password",
        "passwd",
        "secret",
        "api_key",
        "apikey",
        # Candidate personal identifiers (docs/01 §9 — PII is never logged).
        "email",
        "phone",
        "phone_number",
        "telegram",
        "telegram_id",
        "full_name",
        "first_name",
        "last_name",
        "candidate_name",
        "candidate_email",
        "candidate_phone",
        "resume_text",
        "compact_resume",
        "cover_letter",
    }
)

#: Keys whose values are logged without email/phone scanning.
_TECHNICAL_TEXT_KEYS: frozenset[str] = frozenset(
    {
        "event",
        "error_code",
        "error_type",
        "status",
        "request_id",
        "logger",
        "level",
        "timestamp",
        "queue",
        "endpoint",
        "method",
        "task_type",
    }
)

#: Max length of a logged text value (protects against huge payloads).
_MAX_TEXT_LENGTH = 512

#: Max recursion depth while sanitizing nested structures.
_MAX_SANITIZE_DEPTH = 6

#: Current request/WebSocket-session id (contextvar: safe under asyncio).
REQUEST_ID_CTX: contextvars.ContextVar[str] = contextvars.ContextVar(
    "career_request_id", default=""
)

_JWT_RE = re.compile(r"(Bearer|TOKEN|Token|token)\s+[A-Za-z0-9._-]{8,}(?:\.[A-Za-z0-9._-]+)+")
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_PHONE_RE = re.compile(r"(?:\+?\d[\d\s\-().]{7,}\d)")
_SECRET_ASSIGN_RE = re.compile(
    r"(?i)\b(jwt_secret|password|passwd|secret|token|api_key)\b(\s*[=:]\s*)(\S+)"
)
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")

#: ISO-8601 timestamp: not a phone number, must not be masked by the phone regex.
_ISO_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2})?")

#: Standard LogRecord attributes excluded from the JSON payload.
_RECORD_KEYS: frozenset[str] = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "message",
        "module",
        "msecs",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "taskName",
        "thread",
        "threadName",
    }
)


# --- request_id ------------------------------------------------------------------


def new_request_id() -> str:
    """Generate a new short unique request/WebSocket-session id."""
    return uuid.uuid4().hex[:16]


def get_request_id() -> str:
    """Current request_id from context (empty string outside a request)."""
    try:
        return REQUEST_ID_CTX.get() or ""
    except LookupError:  # pragma: no cover - context exists after import
        return ""


def set_request_id(value: str | None = None) -> str:
    """Set request_id in context; empty value is replaced by a fresh one."""
    request_id = str(value or "").strip() or new_request_id()
    REQUEST_ID_CTX.set(request_id)
    try:
        import structlog

        structlog.contextvars.bind_contextvars(request_id=request_id)
    except Exception:  # noqa: BLE001 - structlog is optional
        pass
    return request_id


def bind_request_id(request_id: str | None = None, **context: Any) -> str:
    """Set request_id plus extra (sanitized) bound logger context."""
    resolved = set_request_id(request_id)
    if context:
        try:
            import structlog

            structlog.contextvars.bind_contextvars(**sanitize_payload(context))
        except Exception:  # noqa: BLE001 - structlog is optional
            pass
    return resolved


def clear_request_id() -> None:
    """Reset request_id context (between requests in tests/workers)."""
    REQUEST_ID_CTX.set("")
    try:
        import structlog

        structlog.contextvars.clear_contextvars()
    except Exception:  # noqa: BLE001 - structlog is optional
        pass


# --- Sanitization ----------------------------------------------------------------


def mask_token(value: Any) -> Any:
    """Mask JWT/bearer tokens inside an arbitrary value."""
    if not isinstance(value, str):
        return value
    return _JWT_RE.sub(rf"\1 {MASKED}", value)


def _mask_phone(match: re.Match[str]) -> str:
    digits = re.sub(r"\D", "", match.group(0))
    return MASKED if len(digits) >= 7 else match.group(0)


def _sanitize_text(value: str, *, scan_pii: bool = True) -> str:
    """Sanitize a string: tokens, PII patterns, secrets, length."""
    text = mask_token(value)
    text = _SECRET_ASSIGN_RE.sub(rf"\1\2{MASKED}", text)
    if scan_pii and not _ISO_TIMESTAMP_RE.match(text):
        text = _EMAIL_RE.sub(MASKED, text)
        text = _PHONE_RE.sub(_mask_phone, text)
    text = _CONTROL_CHARS_RE.sub(" ", text)
    if len(text) > _MAX_TEXT_LENGTH:
        text = text[:_MAX_TEXT_LENGTH] + "...<truncated>"
    return text


def sanitize_headers(headers: Any) -> dict[str, str]:
    """Mask sensitive headers (Authorization, Cookie, ...)."""
    if headers is None:
        return {}
    if hasattr(headers, "items"):
        items = list(headers.items())
    elif isinstance(headers, (list, tuple)):
        items = [(str(key), value) for key, value in headers]
    else:
        return {}
    clean: dict[str, str] = {}
    for key, value in items:
        name = str(key)
        if name.strip().lower() in SENSITIVE_HEADERS:
            clean[name] = MASKED
        else:
            clean[name] = _sanitize_text(str(value), scan_pii=True)
    return clean


def sanitize_payload(payload: Any, *, _depth: int = 0) -> Any:
    """Recursively sanitize a structure before it is written to the log.

    - keys from :data:`SENSITIVE_KEYS` become ``***MASKED***``;
    - technical keys are logged without email/phone scanning;
    - other strings pass through :func:`_sanitize_text`;
    - ``None``/numbers/booleans are preserved.
    """
    if _depth > _MAX_SANITIZE_DEPTH:  # guard against cyclic/deep structures
        return "<max-depth>"
    if payload is None or isinstance(payload, bool | int | float):
        return payload
    if isinstance(payload, str):
        return _sanitize_text(payload)
    if isinstance(payload, bytes | bytearray):
        return f"<{len(payload)} bytes>"
    if isinstance(payload, dict):
        clean: dict[str, Any] = {}
        for key, value in payload.items():
            name = str(key)
            lowered = name.strip().lower()
            if lowered in SENSITIVE_KEYS:
                clean[name] = MASKED
            elif lowered in _TECHNICAL_TEXT_KEYS:
                clean[name] = _sanitize_text(str(value), scan_pii=False)
            else:
                clean[name] = sanitize_payload(value, _depth=_depth + 1)
        return clean
    if isinstance(payload, list | tuple | set | frozenset):
        items = list(payload)
        return [sanitize_payload(item, _depth=_depth + 1) for item in items[:100]]
    if isinstance(payload, uuid.UUID):
        return str(payload)
    if isinstance(payload, datetime):
        return payload.isoformat()
    return _sanitize_text(str(payload))


# --- Formatters ------------------------------------------------------------------


def _record_to_dict(record: logging.LogRecord) -> dict[str, Any]:
    """Convert a stdlib LogRecord into a dict for JSON output.

    Works both for plain ``logging`` calls (``extra={"context": {...}}``) and
    for records emitted through ``structlog.stdlib.ProcessorFormatter``, whose
    event fields are stored directly on the record.
    """
    import traceback

    # Structured context passed via ``extra={"context": ...}``.
    context: dict[str, Any] = {}
    raw_extra = getattr(record, "context", None)
    if isinstance(raw_extra, dict):
        context.update(raw_extra)

    # Structlog event fields (ProcessorFormatter stores them on the record).
    for key, value in record.__dict__.items():
        if key in _RECORD_KEYS or key.startswith("_") or key in context:
            continue
        context[key] = value

    message = context.pop("event", None)
    if message is None:
        message = record.getMessage()

    payload: dict[str, Any] = {
        "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
        "level": record.levelname,
        "logger": record.name,
        "event": _sanitize_text(str(message), scan_pii=True),
    }
    if context:
        payload.update(sanitize_payload(context))
    request_id = payload.get("request_id") or get_request_id()
    if request_id:
        payload["request_id"] = request_id
    if record.exc_info:
        exc_type, exc_value, exc_tb = record.exc_info
        payload["exception"] = _sanitize_text(
            "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        )
    elif record.exc_text:
        payload["exception"] = _sanitize_text(record.exc_text)
    if record.stack_info:
        payload["stack"] = _sanitize_text(record.stack_info)
    return payload


class JsonFormatter(logging.Formatter):
    """stdlib JSON formatter (fallback when structlog is unavailable)."""

    def format(self, record: logging.LogRecord) -> str:
        try:
            return json.dumps(_record_to_dict(record), ensure_ascii=False, default=str)
        except Exception:  # noqa: BLE001 - logging must never raise
            return json.dumps(
                {"level": record.levelname, "logger": record.name, "event": MASKED},
                ensure_ascii=False,
            )


class RequestIdFilter(logging.Filter):
    """Inject request_id from the context into stdlib log records."""

    def filter(self, record: logging.LogRecord) -> bool:
        if not getattr(record, "request_id", None):
            record.request_id = get_request_id()  # type: ignore[attr-defined]
        return True


# --- structlog -------------------------------------------------------------------


def sanitize_event_dict(
    _logger: Any, _method_name: str, event_dict: dict[str, Any]
) -> dict[str, Any]:
    """structlog processor: sanitize PII in every field of the event."""
    sanitized = sanitize_payload(dict(event_dict))
    return sanitized if isinstance(sanitized, dict) else dict(event_dict)


def _structlog_processors(structlog: Any) -> list[Any]:
    """Shared processor chain: contextvars, level, timestamp, sanitization."""
    return [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.UnicodeDecoder(),
        structlog.processors.format_exc_info,
        sanitize_event_dict,
    ]


def _configure_structlog(structlog: Any, *, json_output: bool, level: str) -> Any:
    """Configure structlog and return the handler formatter to use.

    Structlog events are handed over to the stdlib logging stack through
    ``structlog.stdlib.ProcessorFormatter``, so a single handler renders both
    structlog events and plain stdlib records into one JSON line.
    """
    numeric = level if isinstance(level, int) else logging.getLevelName(str(level).upper())
    if not isinstance(numeric, int):
        numeric = logging.INFO

    renderer: Any = structlog.processors.JSONRenderer(ensure_ascii=False)
    if not json_output:
        renderer = structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())

    structlog.configure(
        processors=[
            *_structlog_processors(structlog),
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )
    return structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=_structlog_processors(structlog),
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
    )


def configure_logging(*, level: str = "INFO", json_output: bool = True) -> None:
    """Configure the root stdlib logger and structlog.

    Args:
        level: log level (DEBUG/INFO/WARNING/ERROR).
        json_output: ``True`` - JSON (production), ``False`` - console (dev).
    """
    numeric = level if isinstance(level, int) else logging.getLevelName(str(level).upper())
    if not isinstance(numeric, int):
        numeric = logging.INFO

    formatter: logging.Formatter = JsonFormatter()
    try:
        import structlog

        formatter = _configure_structlog(structlog, json_output=json_output, level=level)
    except Exception:  # noqa: BLE001 - structlog is optional
        pass

    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(numeric)
    handler.setFormatter(formatter)
    handler.addFilter(RequestIdFilter())

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(numeric)

    # uvicorn duplicates output through its own handler - route it to root.
    for name in ("uvicorn", "uvicorn.access", "uvicorn.error", "sqlalchemy.engine"):
        child = logging.getLogger(name)
        child.handlers.clear()
        child.propagate = True
        if not any(isinstance(f, RequestIdFilter) for f in child.filters):
            child.addFilter(RequestIdFilter())


class _BoundLogger:
    """Minimal stand-in for structlog BoundLogger when structlog is absent."""

    def __init__(self, logger: logging.Logger, context: dict[str, Any]) -> None:
        self._logger = logger
        self._context = dict(context)

    def bind(self, **kwargs: Any) -> _BoundLogger:
        merged = dict(self._context)
        merged.update(kwargs)
        return _BoundLogger(self._logger, merged)

    def unbind(self, *keys: str) -> _BoundLogger:
        context = {k: v for k, v in self._context.items() if k not in keys}
        return _BoundLogger(self._logger, context)

    def _log(self, level: int, event: str, **kwargs: Any) -> None:
        merged = {**self._context, **kwargs}
        self._logger.log(level, event, extra={"context": merged})

    def debug(self, event: str, **kwargs: Any) -> None:
        self._log(logging.DEBUG, event, **kwargs)

    def info(self, event: str, **kwargs: Any) -> None:
        self._log(logging.INFO, event, **kwargs)

    def warning(self, event: str, **kwargs: Any) -> None:
        self._log(logging.WARNING, event, **kwargs)

    def error(self, event: str, **kwargs: Any) -> None:
        self._log(logging.ERROR, event, **kwargs)

    def critical(self, event: str, **kwargs: Any) -> None:
        self._log(logging.CRITICAL, event, **kwargs)

    def exception(self, event: str, **kwargs: Any) -> None:
        merged = {**self._context, **kwargs}
        self._logger.error(event, exc_info=True, extra={"context": merged})


def get_logger(name: str | None = None, **context: Any) -> Any:
    """Return a structured logger bound to the current request_id."""
    logger_name = name or "career"
    try:
        import structlog

        logger = structlog.get_logger(logger_name)
    except Exception:  # noqa: BLE001 - structlog is optional
        return _BoundLogger(logging.getLogger(logger_name), dict(context))

    if context:
        logger = logger.bind(**sanitize_payload(context))
    request_id = get_request_id()
    if request_id:
        logger = logger.bind(request_id=request_id)
    return logger
