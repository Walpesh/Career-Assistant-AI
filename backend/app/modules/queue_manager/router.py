"""Queue Manager — точка подключения роутера (docs/03_API_CONTRACTS.md §7).

Контракт:
    GET  /tasks                — список задач пользователя
    GET  /tasks/{task_id}      — статус конкретной задачи
    POST /tasks/{task_id}/cancel — отменить задачу (если возможно)

Типы задач (docs/02_DATABASE.md §3.6):
    parse_auto / parse_group / parse_manual / analyze /
    generate_letter / auto_full / convert_resume

Правила: LLM — строго 1 воркер; парсинг — ≤ 2 воркера на пользователя;
прогресс публикуется в Realtime Module (событие task.progress).
"""

from fastapi import APIRouter

router = APIRouter(prefix="/tasks", tags=["tasks"])
