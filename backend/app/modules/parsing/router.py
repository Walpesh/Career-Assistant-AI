"""Parsing Orchestrator — точка подключения роутера (docs/03_API_CONTRACTS.md §5).

Контракт (все возвращают { task_id, status: "pending" }):
    POST /parsing/auto   — Автопоиск: { keywords[], employment_forms[],
                            work_formats[], schedules[], match_threshold, max_pages }
    POST /parsing/group  — Групповой парсер: { search_url, max_pages }
    POST /parsing/manual — Ручное добавление: { vacancy_url, run_analysis }

Правила и лимиты: docs/04_PARSING_RULES.md
(приоритет очереди: ручное → группа → авто; ≤ 2 воркера на пользователя).
"""

from fastapi import APIRouter

router = APIRouter(prefix="/parsing", tags=["parsing"])
