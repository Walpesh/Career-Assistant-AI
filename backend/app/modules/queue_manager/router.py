"""Queue Manager — точка подключения роутера (docs/03_API_CONTRACTS.md §7).

Контракт:
    GET  /tasks                — список задач пользователя
    GET  /tasks/{task_id}      — статус конкретной задачи
    POST /tasks/{task_id}/cancel — отменить задачу (если возможно)
    POST /tasks/{task_id}/resume — возобновить waiting_captcha после капчи

Типы задач (docs/02_DATABASE.md §3.6):
    parse_auto / parse_group / parse_manual / analyze /
    generate_letter / auto_full / convert_resume

Правила: LLM — строго 1 воркер; парсинг — ≤ 2 воркера на пользователя;
прогресс публикуется в Realtime Module (событие task.progress).
Отмена дополнительно снимает job из очереди Redis (чтобы воркер не подхватил
отменённую задачу) и публикует task.cancelled/task.failed; возобновление после
капчи возвращает задачу в очередь и публикует task.resumed.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Path, Query, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.db.models import Task, Vacancy
from app.db.session import get_db
from app.modules.auth.deps import get_current_user
from app.modules.queue_manager.queues import (
    QueueUnavailable,
    abort_job,
    enqueue_task,
    queue_for_task_type,
)
from app.modules.realtime.bus import publish_event

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


async def _get_user_task(db: AsyncSession, task_id: uuid.UUID, user) -> Task:
    """Задача текущего пользователя; чужая/неизвестная → 404 (IDOR-защита)."""
    task = await db.get(Task, task_id)
    if task is None or task.user_id != user.id:
        raise AppError(404, "Задача не найдена", "NOT_FOUND")
    return task


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
    return await _get_user_task(db, task_id, user)


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
    """Отменить задачу (pending / processing / waiting_captcha).

    - job явно снимается из очереди Redis — отмена не ждёт воркера;
    - отмена блокируется (409), если связанная вакансия удалена или её
      статус уже `applied` (терминальный статус, docs/02 §5);
    - публикуются события `task.cancelled` и `task.failed`, чтобы UI обновился
      мгновенно (docs/03 §8).
    """
    task = await _get_user_task(db, task_id, user)

    if task.status == "completed":
        raise AppError(400, "Задача уже завершена", "TASK_COMPLETED")

    if task.status == "failed":
        raise AppError(400, "Задача завершилась ошибкой", "TASK_FAILED")

    # Блокировка отмены: вакансия удалена или уже откликнулась (applied).
    if task.related_vacancy_id is not None:
        vacancy = await db.get(Vacancy, task.related_vacancy_id)
        if vacancy is None:
            raise AppError(
                409,
                "Связанная вакансия удалена — отмена задачи невозможна",
                "VACANCY_DELETED",
            )
        if vacancy.status == "applied":
            raise AppError(
                409,
                "Вакансия уже в статусе applied — отмена задачи запрещена",
                "VACANCY_APPLIED",
            )

    # 1) Снимаем job из очереди Redis, чтобы воркер не начал/не продолжил работу.
    await abort_job(task.id, task.task_type)

    # 2) Фиксируем отмену в БД (timezone-aware now, docs/02 §3.6).
    task.status = "failed"
    task.error_message = "Отменено пользователем"
    task.finished_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(task)

    # 3) Мгновенное обновление UI (docs/03 §8).
    await publish_event(
        str(user.id),
        "task.cancelled",
        {
            "task_id": str(task.id),
            "task_type": task.task_type,
            "status": task.status,
            "error": task.error_message,
        },
    )
    await publish_event(
        str(user.id),
        "task.failed",
        {"task_id": str(task.id), "error": task.error_message},
    )

    return {"task_id": str(task.id), "status": task.status, "cancelled": True}


@router.post(
    "/{task_id}/resume",
    status_code=status.HTTP_200_OK,
    summary="Возобновить задачу после капчи (docs/03 §7, docs/04 §2 п.3)",
)
async def resume_task(
    task_id: uuid.UUID = Path(..., description="UUID задачи"),
    user = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Вернуть ``waiting_captcha``-задачу в очередь после обхода капчи.

    Статус меняется на ``pending``, job ставится в Redis заново (при живом
    «старом» ключе — с уникальным attempt-суффиксом, чтобы дедупликация не
    потеряла задачу) и публикуется событие `task.resumed`.
    """
    task = await _get_user_task(db, task_id, user)

    if task.status != "waiting_captcha":
        raise AppError(
            409,
            "Возобновить можно только задачу в статусе waiting_captcha",
            "TASK_NOT_WAITING_CAPTCHA",
        )

    task.status = "pending"
    task.error_message = None
    task.finished_at = None
    task.started_at = None
    await db.commit()
    await db.refresh(task)

    try:
        job_id = await enqueue_task(task.id, task.task_type)
        if job_id is None:
            # «Старый» job ещё жив в Redis — снимаем и ставим с суффиксом.
            await abort_job(task.id, task.task_type)
            job_id = await enqueue_task(
                task.id,
                task.task_type,
                attempt=f"resume-{uuid.uuid4().hex[:8]}",
            )
    except QueueUnavailable as exc:
        task.status = "waiting_captcha"  # откат: очередь недоступна
        task.error_message = f"Очередь задач недоступна: {exc}"
        await db.commit()
        raise AppError(
            503,
            "Очередь задач недоступна, повторите попытку позже",
            "QUEUE_UNAVAILABLE",
        ) from exc

    if job_id is None:
        raise AppError(
            503,
            "Не удалось вернуть задачу в очередь, повторите попытку позже",
            "QUEUE_UNAVAILABLE",
        )

    await publish_event(
        str(user.id),
        "task.resumed",
        {
            "task_id": str(task.id),
            "task_type": task.task_type,
            "status": task.status,
            "queue": queue_for_task_type(task.task_type),
        },
    )
    return {"task_id": str(task.id), "status": task.status, "resumed": True}
