"""Сборка роутеров всех модулей Modular-Flow (docs/03_API_CONTRACTS.md).

Маршруты:
    /auth      — Auth Module (§2)
    /profile   — User Profile Module (§3)
    /vacancies — Vacancy Storage Module (§4)
    /parsing   — Parsing Orchestrator (§5)
    /analysis  — Analysis & Letter Module, анализ (§6)
    /letters   — Analysis & Letter Module, письма (§6)
    /tasks     — Queue Manager (§7)
    /ws        — Realtime & Notification Module (§8)

Proxy & Anti-Ban Module HTTP-эндпоинтов не имеет — внутренний модуль.
"""

from fastapi import APIRouter

from app.modules.analysis_letter.router import analysis_router, letters_router
from app.modules.auth.router import router as auth_router
from app.modules.parsing.router import router as parsing_router
from app.modules.queue_manager.router import router as tasks_router
from app.modules.realtime.router import router as realtime_router
from app.modules.user_profile.router import router as profile_router
from app.modules.vacancy_storage.router import router as vacancies_router

api_router = APIRouter()
api_router.include_router(auth_router)
api_router.include_router(profile_router)
api_router.include_router(parsing_router)
api_router.include_router(vacancies_router)
api_router.include_router(analysis_router)
api_router.include_router(letters_router)
api_router.include_router(tasks_router)
api_router.include_router(realtime_router)
