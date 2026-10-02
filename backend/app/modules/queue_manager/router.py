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

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, Path, Query, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Task
from app.db.session import get_db
from app.modules.auth.deps import get_current_user

router = APIRouter(prefix="/tasks", tags=["tasks"])


# --- Schemas ---

class TaskStatus(str):
    """Статусы задач (docs/02_DATABASE.md §3.6)."""
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    WAITING_CAPTCHA = "waiting_captcha"


class TaskType(str):
    """Типы задач (docs/02_DATABASE.md §3.6)."""
    PARSE_AUTO = "parse_auto"
    PARSE_GROUP = "parse_group"
    PARSE_MANUAL = "parse_manual"
    ANALYZE = "analyze"
    GENERATE_LETTER = "generate_letter"
    AUTO_FULL = "auto_full"
    CONVERT_RESUME = "convert_resume"


class TaskOut(BaseModel):
    """Ответ GET /tasks/{task_id} — детальная информация о задаче."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    task_type: str
    status: str
    progress_current: int
    progress_total: int
    progress_message: str | None = None
    progress_stage: str | None = None
    payload: dict | None = None
    result: dict | None = None
    error_message: str | None = None
    related_vacancy_id: uuid.UUID | None = None
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None


class TaskListOut(BaseModel):
    """Ответ GET /tasks — список задач пользователя (пагинация)."""

    items: list[TaskOut]
    total: int = Field(default=0)
    page: int = Field(default=1)
    size: int = Field(default=20)


class TaskListOutList(BaseModel):
    """Альтернативный формат списка задач — массив без метаданных."""
    items: list[TaskOut]


# --- Endpoints ---

@router.get(
    "",
    response_model=TaskListOut,
    summary="Список задач пользователя (docs/03 §7)",
)
async def list_tasks(
    user = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    page: int = Query(1, ge=1, description="Номер страницы"),
    size: int = Query(20, ge=1, le=100, description="Размер страницы"),
) -> TaskListOut:
    """Получить список задач текущего пользователя с пагинацией."""

    total = await db.scalar(
        select(func.count())
        .select_from(Task)
        .where(Task.user_id == user.id)
    )

    count = total or 0

    rows = await db.scalars(
        select(Task)
        .where(Task.user_id == user.id)
        .order_by(Task.created_at.desc())
        .offset((page - 1) * size)
        .limit(size)
    )

    items = list(rows.all())

    return TaskListOut(
        items=items,
        total=count,
        page=page,
        size=size,
    )


@router.get(
    "/{task_id}",
    response_model=TaskOut,
    summary="Статус конкретной задачи (docs/03 §7)",
)
async def get_task(
    task_id: uuid.UUID = Path(..., description="UUID задачи"),
    user = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> TaskOut:
    """Получить статус конкретной задачи."""
    task = await db.get(Task, task_id)
    if task is None or task.user_id != user.id:
        from app.core.errors import AppError
        raise AppError(404, "Задача не найдена", "NOT_FOUND")
    return task


@router.post(
    "/{task_id}/cancel",
    status_code=status.HTTP_200_OK,
    summary="Отменить задачу (если возможно) (docs/03 §7)",
)
async def cancel_task(
    task_id: uuid.UUID = Path(..., description="UUID задачи"),
    user = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Отменить задачу. Можно отменить только задачи в статусах pending, processing, waiting_captcha."""
    task = await db.get(Task, task_id)
    if task is None or task.user_id != user.id:
        from app.core.errors import AppError
        raise AppError(404, "Задача не найдена", "NOT_FOUND")
    
    if task.status == "completed":
        from app.core.errors import AppError
        raise AppError(400, "Задача уже завершена", "TASK_COMPLETED")
    
    if task.status == "failed":
        from app.core.errors import AppError
        raise AppError(400, "Задача завершилась ошибкой", "TASK_FAILED")
    
    # Отменяем задачу
    task.status = "failed"
    task.error_message = "Отменено пользователем"
    task.finished_at = datetime.utcnow()
    
    await db.commit()
    await db.refresh(task)
    return {"task_id": str(task.id), "status": task.status, "cancelled": True}
