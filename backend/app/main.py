"""FastAPI-приложение Career-Assistant-AI (Modular-Flow).

Точка входа: `uvicorn app.main:app`.
- /api/v1 — REST-контракты (docs/03_API_CONTRACTS.md) через модули app.modules.*;
- /ws    — WebSocket реал-тайм событий (Realtime & Notification Module);
- /health — проверка живости;
- /      — отдача frontend/ (если каталог существует).
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.httpsredirect import HTTPSRedirectMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.api.v1.router import api_router
from app.core.config import settings
from app.core.errors import DEFAULT_ERROR_CODES, AppError
from app.core.logging import configure_logging, get_logger
from app.core.rate_limit import RateLimitMiddleware
from app.core.request_id import RequestIDMiddleware
from app.core.security_headers import SecurityHeadersMiddleware
from app.core.sentry import capture_exception, init_sentry
from app.modules.metrics import MetricsMiddleware, start_collector, stop_collector
from app.modules.metrics.router import router as metrics_router

#: Structured JSON logger (docs/01 §9): request_id + PII sanitization.
logger = get_logger(__name__)

#: stdlib logger kept for interop with libraries injecting ``extra``.
_stdlib_logger = logging.getLogger(__name__)

#: JSON-логирование настраивается один раз при импорте приложения.
configure_logging(level=settings.log_level, json_output=settings.log_json_output)

#: Sentry включается только при заданном SENTRY_DSN (иначе — no-op, PII-safe).
SENTRY_ENABLED = init_sentry(
    dsn=settings.sentry_dsn,
    environment=settings.environment,
    release=settings.app_version,
    traces_sample_rate=settings.sentry_traces_sample_rate,
)


def _frontend_dir() -> Path:
    """Каталог frontend: backend/app → корень репо (dev) или /app (Docker)."""
    repo_root = Path(__file__).resolve().parents[2]
    candidates = (
        repo_root / "frontend",  # dev: backend/app/main.py → корень/frontend
        Path("/app/frontend"),  # Docker: WORKDIR /app + COPY frontend
    )
    for path in candidates:
        if path.exists():
            return path
    return candidates[0]


FRONTEND_DIR = _frontend_dir()


class SensitiveHeaderLogFilter:
    """Фильтр для uvicorn-логгера, замаскировывающий чувствительные данные."""

    def filter(self, record):  # noqa: A002
        """Фильтрует записи журнала, замаскируя токены в сообщениях."""
        if hasattr(record, "msg"):
            # Замаскировать токены в сообщениях
            msg = str(record.msg)
            # Маскируем JWT-токены в формате Bearer <token>
            import re
            msg = re.sub(
                r"(Bearer|TOKEN|token|Token)\s+[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+",
                r"\1 ***MASKED***",
                msg,
            )
            record.msg = msg
        return True


def _error_response(status_code: int, detail: str, error_code: str | None) -> JSONResponse:
    """Единый формат ошибок: { detail, error_code } (docs/03_API_CONTRACTS.md §1)."""
    return JSONResponse(
        status_code=status_code,
        content={
            "detail": detail,
            "error_code": error_code or DEFAULT_ERROR_CODES.get(status_code, "ERROR"),
        },
    )


def _validation_detail(exc: RequestValidationError) -> str:
    """Человекочитаемое сообщение из pydantic/Starlette ошибок валидации."""
    parts: list[str] = []
    for error in exc.errors():
        location = ".".join(str(item) for item in error.get("loc", ()) if item != "body")
        message = error.get("msg", "Некорректное значение")
        parts.append(f"{location}: {message}" if location else message)
    return "Ошибка валидации: " + "; ".join(parts) if parts else "Ошибка валидации"


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Жизненный цикл: Redis-очередь и ARQ-воркеры Queue Manager (docs/04 §6).

    DB short-polling отсутствует: задачи попадают в Redis при создании через
    `enqueue_job`, а воркеры забирают их настоящим чтением очереди ARQ.
    Поэтому в состоянии простоя SQL-запросов к `tasks` нет вообще.

    Production: встроенные воркеры запрещены (QUEUE_EMBEDDED_WORKERS=false,
    fail-fast в Settings) — API и воркеры работают независимыми рантаймами
    (worker-parsing ×N, worker-llm строго ×1). Восстановление зависших задач
    защищено Redis-блокировкой SET NX EX — выполняет один процесс.
    """
    from app.db.session import AsyncSessionLocal
    from app.modules.queue_manager.queues import (
        close_pool,
        get_pool,
        recover_pending_tasks,
        start_embedded_workers,
        stop_embedded_workers,
    )
    from app.modules.realtime.bus import RealtimeBridge

    bridge = RealtimeBridge()
    redis_available = await get_pool() is not None

    # Разовая реанимация задач, застрявших в pending/processing после сбоя.
    # Защищена Redis-блокировкой SET NX EX: при старте N реплик API
    # восстановление выполняет только один процесс (остальные — пропуск).
    if redis_available and settings.queue_recover_on_startup:
        try:
            await recover_pending_tasks(AsyncSessionLocal)
        except Exception:  # noqa: BLE001 — старт не должен зависеть от восстановления
            logger.exception("Queue Manager: не удалось восстановить задачи")

    await bridge.start()

    # Dev/staging: воркеры внутри процесса FastAPI. Production: только
    # отдельные рантаймы (worker-parsing ×N, worker-llm ×1) — флаг
    # effective_embedded_workers в production всегда False (defence in depth
    # поверх fail-fast валидатора Settings).
    if settings.metrics_enabled:
        # Периодический сбор длин очередей, слотов семафора и доступности Ollama.
        start_collector()

    if redis_available and settings.effective_embedded_workers:
        await start_embedded_workers(AsyncSessionLocal)
    elif redis_available and settings.is_production:
        logger.info(
            "Queue Manager: production — встроенные воркеры отключены, "
            "задачи исполняют worker-parsing/worker-llm"
        )

    try:
        yield
    finally:
        await stop_embedded_workers()
        await bridge.stop()
        await stop_collector()
        await close_pool()
        logger.info("Queue Manager: остановлен")


def create_app() -> FastAPI:
    # В production отключаем /docs, /redoc и /openapi.json — интерактивная
    # документация не должна раскрывать схему API наружу.
    _prod = settings.is_production
    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        description="Сервис автоматизации поиска, парсинга и анализа вакансий hh.ru + LLM-письма",
        lifespan=lifespan,
        docs_url=None if _prod else "/docs",
        redoc_url=None if _prod else "/redoc",
        openapi_url=None if _prod else "/openapi.json",
    )

    # --- Единый формат ошибок (docs/03 §1: { detail, error_code }) ---
    @app.exception_handler(AppError)
    async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
        return _error_response(exc.status_code, str(exc.detail), exc.error_code)

    @app.exception_handler(HTTPException)
    async def http_error_handler(request: Request, exc: HTTPException) -> JSONResponse:
        code = getattr(exc, "error_code", None)
        return _error_response(exc.status_code, str(exc.detail), code)

    @app.exception_handler(StarletteHTTPException)
    async def starlette_error_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = getattr(exc, "error_code", None)
        if exc.status_code >= 500:
            # 5xx — инцидент: уходит в Sentry (PII вычищается before_send).
            capture_exception(exc)
        return _error_response(exc.status_code, str(exc.detail), code)

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        # docs/03 §9: 400 — ошибка валидации.
        return _error_response(400, _validation_detail(exc), "VALIDATION_ERROR")

    # --- Middleware stack ---
    # Порядок добавления: последний добавленный middleware — самый внешний.
    # Итоговый порядок снаружи внутрь: SecurityHeaders → TrustedHost →
    # HTTPSRedirect(prod) → CORS → RateLimit → GZip → приложение.
    # SecurityHeaders снаружи, чтобы заголовки были и на 429/400-ответах.

    # Метрики HTTP-латентности по endpoint (docs/01 §9).
    if settings.metrics_enabled:
        app.add_middleware(MetricsMiddleware)

    # request_id в каждый HTTP-запрос и WS-сессию + заголовок X-Request-ID (docs/01 §9).
    app.add_middleware(RequestIDMiddleware)

    # GZip-сжатие (docs/03 — уменьшение трафика JSON-ответов).
    app.add_middleware(GZipMiddleware, minimum_size=settings.gzip_min_size)

    # Redis sliding-window rate limiting (429 при превышении лимитов).
    app.add_middleware(RateLimitMiddleware)

    # CORS: только явные источники; методы/заголовки — белые списки.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=settings.cors_allow_credentials,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "Accept", "X-Requested-With"],
    )

    if _prod:
        # За TLS-терминатором: принудительный редирект http → https.
        app.add_middleware(HTTPSRedirectMiddleware)

    # Защита от Host-спуфинга: разрешены только известные хосты.
    allowed_hosts = settings.trusted_host_list
    if allowed_hosts:
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts)

    # Security-заголовки (CSP, HSTS, X-Frame-Options, Referrer-Policy, …).
    app.add_middleware(SecurityHeadersMiddleware)

    app.include_router(api_router, prefix=settings.api_prefix)

    # Prometheus-метрики: GET /metrics, /metrics/summary, /metrics/alerts.
    app.include_router(metrics_router)

    @app.get("/health", tags=["system"])
    async def health() -> dict[str, str]:
        """Проверка живости сервиса."""
        return {"status": "ok", "app": settings.app_name, "version": settings.app_version}

    @app.get("/health/live", tags=["system"])
    async def health_live() -> dict[str, str]:
        """Liveness: процесс жив и обрабатывает запросы (Kubernetes livenessProbe)."""
        return {"status": "ok", "app": settings.app_name, "version": settings.app_version}

    @app.get("/health/ready", tags=["system"])
    async def health_ready() -> JSONResponse:
        """Readiness: Postgres (SELECT 1), Redis (PING), Ollama (коннект).

        Kubernetes readinessProbe: 200 — под принимает трафик, 503 — нет.
        """
        from app.modules.health.checks import check_readiness

        report = await check_readiness()
        status_code = 200 if report["status"] == "ready" else 503
        return JSONResponse(status_code=status_code, content=report)

    # Frontend отдаётся тем же приложением (когда каталог существует).
    if FRONTEND_DIR.exists():
        app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")

    return app


app = create_app()
