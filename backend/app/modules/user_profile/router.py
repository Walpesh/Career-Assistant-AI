"""User Profile Module — точка подключения роутера (docs/03_API_CONTRACTS.md §3).

Контракт:
    GET  /profile                 — получить профиль
    PUT  /profile                 — обновить (частичное обновление поддерживается)
    POST /profile/convert-resume  — запуск сокращения резюме через LLM
                                     (задача convert_resume → Queue Manager)

Таблица: user_profiles (docs/02_DATABASE.md §3.2).
Промпт конвертации: docs/05_LLM_PIPELINE.md §3 (результат → compact_resume).
"""

from fastapi import APIRouter

router = APIRouter(prefix="/profile", tags=["profile"])
