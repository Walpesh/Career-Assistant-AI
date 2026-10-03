"""User Profile Module — endpoints (docs/03_API_CONTRACTS.md §3).

Контракт:
    GET  /profile               — получить профиль
    PUT  /profile               — обновить (частичное обновление поддерживается)
    POST /profile/convert-resume  — сокращение резюме через LLM (docs/03 §3):
        ставит задачу convert_resume в очередь Queue Manager → 202 {task_id, status}
    POST /profile/compress-resume — синхронный алиас convert-resume

Таблица: user_profiles (docs/02_DATABASE.md §3.2).
Промпт конвертации: docs/05_LLM_PIPELINE.md §3 (результат → compact_resume).
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.errors import AppError
from app.db.models import Task, User, UserProfile
from app.db.session import get_db
from app.modules.auth.deps import get_current_user
from app.modules.billing.middleware import quota_consumed
from app.modules.billing.tiers import QuotaKind
from app.modules.queue_manager.queues import QueueUnavailable, enqueue_task
from app.modules.realtime.bus import publish_event
from app.modules.user_profile.llm import LLMError, compress_resume_text, trim_to_limit
from app.modules.user_profile.schemas import ProfileOut, ProfileUpdate

router = APIRouter(prefix="/profile", tags=["profile"])

# Поля, которые PUT /profile умеет менять (соответствует ProfileUpdate).
_UPDATABLE_FIELDS = (
    "full_name",
    "resume_text",
    "skills",
    "experience_years",
    "desired_salary_from",
    "desired_salary_to",
    "match_threshold",
    "preferred_work_formats",
    "analysis_preferences",
    "resume_addition",
)


async def _get_or_create_profile(db: AsyncSession, user: User) -> UserProfile:
    """Профиль пользователя; если строки нет (старый аккаунт) — создаётся."""
    profile = await db.get(UserProfile, user.id)
    if profile is None:
        profile = UserProfile(user_id=user.id)
        db.add(profile)
        await db.commit()
        # server_default created_at/updated_at — подгружаем явно (sync-контекст
        # сериализации не может выполнить ленивую загрузку).
        await db.refresh(profile)
    return profile


@router.get("", response_model=ProfileOut, summary="Получить профиль")
async def get_profile(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> UserProfile:
    """Полный профиль текущего пользователя (docs/03 §3)."""
    return await _get_or_create_profile(db, user)


@router.put("", response_model=ProfileOut, summary="Обновить профиль (частично)")
async def update_profile(
    payload: ProfileUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> UserProfile:
    """Частичное обновление: применяются только присланные поля (docs/03 §3)."""
    profile = await _get_or_create_profile(db, user)

    # Явно перечисляем поля: null очищает значение, отсутствие — не трогает.
    changed = payload.model_dump(exclude_unset=True)
    for field in _UPDATABLE_FIELDS:
        if field in changed:
            setattr(profile, field, changed[field])

    await db.commit()
    await db.refresh(profile)  # updated_at (onupdate) — без ленивой загрузки
    return profile


class TaskPendingOut(BaseModel):
    """Ответ POST /profile/convert-resume — задача поставлена в очередь (202)."""

    model_config = ConfigDict(from_attributes=True)

    task_id: str
    status: str = "pending"


async def _compress(user: User, db: AsyncSession) -> UserProfile:
    """Общая логика convert/compress-resume (docs/05 §3)."""
    profile = await _get_or_create_profile(db, user)

    resume_text = (profile.resume_text or "").strip()
    if not resume_text:
        raise AppError(
            status.HTTP_400_BAD_REQUEST,
            "Сначала заполните resume_text — сжимать нечего",
            "RESUME_EMPTY",
        )

    try:
        compact = await compress_resume_text(
            resume_text, max_chars=settings.compact_resume_max_chars
        )
    except LLMError as exc:
        raise AppError(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            f"Не удалось сократить резюме: {exc}",
            "LLM_UNAVAILABLE",
        ) from exc

    # Запись в БД: лимит гарантируется и здесь (единая точка обрезки),
    # поэтому в user_profiles.compact_resume попадает ровно то, что вернул LLM,
    # но не длиннее COMPACT_RESUME_MAX_CHARS.
    profile.compact_resume = trim_to_limit(compact, settings.compact_resume_max_chars)
    await db.commit()
    await db.refresh(profile)
    return profile


@router.post(
    "/convert-resume",
    response_model=TaskPendingOut,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Сократить резюме через LLM → задача в очереди (docs/03 §3)",
)
async def convert_resume(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> TaskPendingOut:
    """Поставить задачу convert_resume в LLM-очередь Queue Manager (docs/01 §4.3).

    Долгая LLM-операция выполняется воркером строго последовательной очереди
    (docs/04 §6): endpoint только создаёт запись в `tasks`, ставит её в Redis и
    отвечает 202 { task_id, status: "pending" }. Результат (compact_resume)
    приходит событием task.completed; прогресс — task.progress.
    """
    profile = await _get_or_create_profile(db, user)

    if not (profile.resume_text or "").strip():
        raise AppError(
            status.HTTP_400_BAD_REQUEST,
            "Сначала заполните resume_text — сжимать нечего",
            "RESUME_EMPTY",
        )

    # Сокращение резюме — LLM-операция, поэтому тратит квоту анализа.
    # Списание до постановки задачи: 429 отдаётся синхронно (docs/03 §11).
    await quota_consumed(db, user, QuotaKind.ANALYSIS)

    task = Task(
        user_id=user.id,
        task_type="convert_resume",
        status="pending",
        progress_current=0,
        progress_total=1,
        payload={},
    )
    db.add(task)
    await db.commit()
    await db.refresh(task)

    try:
        await enqueue_task(task.id, task.task_type)
    except QueueUnavailable as exc:
        task.status = "failed"
        task.error_message = f"Очередь задач недоступна: {exc}"
        task.finished_at = datetime.now(timezone.utc)
        await db.commit()
        raise AppError(
            503,
            "Очередь задач недоступна, повторите попытку позже",
            "QUEUE_UNAVAILABLE",
        ) from exc

    await publish_event(
        str(user.id),
        "task.created",
        {"task_id": str(task.id), "task_type": task.task_type, "status": task.status},
    )
    return TaskPendingOut(task_id=str(task.id), status=task.status)


@router.post(
    "/compress-resume",
    response_model=ProfileOut,
    summary="Сжать резюме через LLM (алиас convert-resume)",
)
async def compress_resume(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> UserProfile:
    """Алиас /profile/convert-resume — «Compress Resume» из TASK (синхронный)."""
    return await _compress(user, db)
