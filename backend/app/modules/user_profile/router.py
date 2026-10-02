"""User Profile Module — endpoints (docs/03_API_CONTRACTS.md §3).

Контракт:
    GET  /profile               — получить профиль
    PUT  /profile               — обновить (частичное обновление поддерживается)
    POST /profile/convert-resume  — сокращение резюме через LLM (docs/03 §3)
    POST /profile/compress-resume — алиас convert-resume (название из TASK)

Таблица: user_profiles (docs/02_DATABASE.md §3.2).
Промпт конвертации: docs/05_LLM_PIPELINE.md §3 (результат → compact_resume).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.core.config import settings
from app.db.models import User, UserProfile
from app.db.session import get_db
from app.modules.auth.deps import get_current_user
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
    response_model=ProfileOut,
    summary="Сократить резюме через LLM → compact_resume",
)
async def convert_resume(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> UserProfile:
    """Запуск сокращения резюме (docs/03 §3, промпт — docs/05 §3).

    Сейчас выполняется синхронно; при появлении Queue Manager задача
    convert_resume будет уходить в LLM Worker (docs/01 §4.3).
    """
    return await _compress(user, db)


@router.post(
    "/compress-resume",
    response_model=ProfileOut,
    summary="Сжать резюме через LLM (алиас convert-resume)",
)
async def compress_resume(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> UserProfile:
    """Алиас /profile/convert-resume — «Compress Resume» из TASK."""
    return await _compress(user, db)
