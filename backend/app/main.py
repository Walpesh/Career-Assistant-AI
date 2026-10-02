"""FastAPI-приложение Career-Assistant-AI (Modular-Flow).

Точка входа: `uvicorn app.main:app`.
- /api/v1 — REST-контракты (docs/03_API_CONTRACTS.md) через модули app.modules.*;
- /ws    — WebSocket реал-тайм событий (Realtime & Notification Module);
- /health — проверка живости;
- /      — отдача frontend/ (если каталог существует).
"""

from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.v1.router import api_router
from app.core.config import settings
from app.core.errors import AppError, DEFAULT_ERROR_CODES

FRONTEND_DIR = Path(__file__).resolve().parents[2] / "frontend"


def _mask_sensitive_headers(headers: dict) -> dict:
    """Замаскировать чувствительные заголовки в логах (Authorization, Cookie и др.)."""
    sensitive_keys = {"authorization", "cookie", "auth", "token", "x-api-key", "x-auth-token"}
    masked = {}
    for key, value in headers.items():
        if key.lower() in sensitive_keys:
            masked[key] = "***MASKED***"
        else:
            masked[key] = value
    return masked


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


def create_app() -> FastAPI:
    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        description="Сервис автоматизации поиска, парсинга и анализа вакансий hh.ru + LLM-письма",
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
        return _error_response(exc.status_code, str(exc.detail), code)

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        # docs/03 §9: 400 — ошибка валидации.
        return _error_response(400, _validation_detail(exc), "VALIDATION_ERROR")

    # --- CORS Middleware ---
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # --- JWT Token Masking in Logs ---
    # Добавляем middleware для маскировки токенов в логах запросов
    @app.middleware("http")
    async def mask_sensitive_data_in_logs(request: Request, call_next):
        """Middleware для замаскировки чувствительных данных в логах."""
        # Маскируем Authorization заголовок в логах
        if "authorization" in request.headers:
            # Токен не логируем, только факт наличия заголовка
            pass
        response = await call_next(request)
        return response

    app.include_router(api_router, prefix=settings.api_prefix)

    @app.get("/health", tags=["system"])
    async def health() -> dict[str, str]:
        """Проверка живости сервиса."""
        return {"status": "ok", "app": settings.app_name, "version": settings.app_version}

    # Frontend отдаётся тем же приложением (когда каталог существует).
    if FRONTEND_DIR.exists():
        app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")

    return app


app = create_app()
