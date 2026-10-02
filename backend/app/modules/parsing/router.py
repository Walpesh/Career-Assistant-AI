"""Parsing Orchestrator — точка подключения роутера (docs/03_API_CONTRACTS.md §5).

Контракт (все возвращают { task_id, status: "pending" }):
    POST /parsing/auto   — Автопоиск: { keywords[], employment_forms[],
                            work_formats[], schedules[], match_threshold, max_pages }
    POST /parsing/group  — Групповой парсер: { search_url, max_pages }
    POST /parsing/manual — Ручное добавление: { vacancy_url, run_analysis }

Правила и лимиты: docs/04_PARSING_RULES.md
(приоритет очереди: ручное → группа → авто; ≤ 2 воркера на пользователя).
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.db.models import Task
from app.db.session import get_db
from app.modules.auth.deps import get_current_user
from app.modules.parsing.schemas import (
    ParseAutoRequest,
    ParseGroupRequest,
    ParseManualRequest,
    ParseTaskResponse,
)

router = APIRouter(prefix="/parsing", tags=["parsing"])


async def _create_task(
    db: AsyncSession,
    user_id: uuid.UUID,
    task_type: str,
    payload: dict | None = None,
    related_vacancy_id: uuid.UUID | None = None,
) -> uuid.UUID:
    """Создать задачу в очереди и вернуть её ID (docs/03 §5, §7)."""
    task = Task(
        user_id=user_id,
        task_type=task_type,
        status="pending",
        progress_current=0,
        progress_total=0,
        payload=payload,
        related_vacancy_id=related_vacancy_id,
    )
    db.add(task)
    await db.commit()
    await db.refresh(task)
    return task.id


@router.post(
    "/auto",
    response_model=ParseTaskResponse,
    summary="Запуск автопоиска по ключевым словам (docs/03 §5)",
)
async def parse_auto(
    payload: ParseAutoRequest,
    user = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ParseTaskResponse:
    """Автопоиск вакансий по ключевым словам и фильтрам."""
    if not payload.keywords and not payload.employment_forms and not payload.work_formats:
        raise AppError(
            400,
            "Не указаны критерии поиска: хотя бы одно из полей keywords/employment_forms/work_formats должно быть заполнено",
            "INVALID_SEARCH_CRITERIA",
        )

    task_payload = payload.model_dump(exclude_none=True)
    task_id = await _create_task(db, user.id, "parse_auto", payload=task_payload)
    return ParseTaskResponse(task_id=str(task_id), status="pending")


@router.post(
    "/group",
    response_model=ParseTaskResponse,
    summary="Запуск группового парсинга по готовой ссылке (docs/03 §5)",
)
async def parse_group(
    payload: ParseGroupRequest,
    user = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ParseTaskResponse:
    """Групповой парсер: обход страниц результатов по готовой ссылке."""
    task_payload = payload.model_dump(exclude_none=True)
    task_id = await _create_task(db, user.id, "parse_group", payload=task_payload)
    return ParseTaskResponse(task_id=str(task_id), status="pending")


@router.post(
    "/manual",
    response_model=ParseTaskResponse,
    summary="Ручное добавление вакансии по ссылке (docs/03 §5)",
)
async def parse_manual(
    payload: ParseManualRequest,
    user = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ParseTaskResponse:
    """Ручное добавление одной вакансии по прямой ссылке."""
    task_payload = payload.model_dump(exclude_none=True)
    task_id = await _create_task(db, user.id, "parse_manual", payload=task_payload)
    return ParseTaskResponse(task_id=str(task_id), status="pending")
