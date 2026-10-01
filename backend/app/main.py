"""FastAPI-приложение Career-Assistant-AI (Modular-Flow).

Точка входа: `uvicorn app.main:app`.
- /api/v1 — REST-контракты (docs/03_API_CONTRACTS.md) через модули app.modules.*;
- /ws    — WebSocket реал-тайм событий (Realtime & Notification Module);
- /health — проверка живости;
- /      — отдача frontend/ (если каталог существует).
"""

from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.api.v1.router import api_router
from app.core.config import settings

FRONTEND_DIR = Path(__file__).resolve().parents[2] / "frontend"


def create_app() -> FastAPI:
    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        description="Сервис автоматизации поиска, парсинга и анализа вакансий hh.ru + LLM-письма",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

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
