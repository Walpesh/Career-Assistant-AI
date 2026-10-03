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
from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.db.models import Task
from app.db.session import get_db
from app.modules.auth.deps import get_current_user
from app.modules.billing.middleware import quota_consumed
from app.modules.billing.tiers import QuotaKind
from app.modules.parsing.schemas import (
    ParseAutoRequest,
    ParseGroupRequest,
    ParseManualRequest,
    ParseTaskResponse,
)
from app.modules.parsing.service import normalize_blacklist
from app.modules.parsing.urls import validate_hh_search_url
from app.modules.queue_manager.queues import QueueUnavailable, enqueue_task
from app.modules.realtime.bus import publish_event
from app.modules.vacancy_storage.service import extract_hh_vacancy_id

router = APIRouter(prefix="/parsing", tags=["parsing"])


def _blacklist_payload(payload) -> dict:
    """Чёрный список для tasks.payload (docs/04 §4.9).

    Тумбер выключен → слова в задачу не попадают вовсе, поэтому парсер
    работает ровно по старым фильтрам и настройкам. Включён, но список
    пуст → фильтр включается и не отсевает ничего (fail-safe).
    """
    if not getattr(payload, "blacklist_enabled", False):
        return {"blacklist_enabled": False, "blacklist_words": []}
    return {
        "blacklist_enabled": True,
        "blacklist_words": normalize_blacklist(payload.blacklist_words),
    }


async def _create_task(
    db: AsyncSession,
    user_id: uuid.UUID,
    task_type: str,
    payload: dict | None = None,
    related_vacancy_id: uuid.UUID | None = None,
) -> uuid.UUID:
    """Создать задачу, поставить её в очередь Redis и вернуть её ID.

    Задача попадает в единую очередь Queue Manager (docs/04 §6): запись в
    `tasks` сразу ставится в Redis через ARQ ``enqueue_job`` — опроса базы
    больше нет. Параллельно публикуется событие `task.created` (docs/03 §8).

    Если Redis недоступен, задача помечается failed с понятной ошибкой и
    отдаётся HTTP 503: молча терять работу нельзя, но и возвращать
    «task_id, pending» без возможности выполнения — тоже неверно.
    """
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
        str(user_id),
        "task.created",
        {"task_id": str(task.id), "task_type": task.task_type, "status": task.status},
    )
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
    """Автопоиск вакансий по ключевым словам и фильтрам.

    Критерии комбинируются: достаточно заполнить любое одно из полей,
    остальные подставляются как дополнительные фильтры поиска.
    """
    if (
        not payload.keywords
        and not payload.employment_forms
        and not payload.work_formats
        and not payload.schedules
    ):
        raise AppError(
            400,
            "Не указаны критерии поиска: хотя бы одно из полей "
            "keywords/employment_forms/work_formats/schedules должно быть заполнено",
            "INVALID_SEARCH_CRITERIA",
        )

    # Списание суточной квоты парсинга ДО создания задачи: 429 отдаётся
    # синхронно, и в БД не появляется задача, которую воркер всё равно
    # не выполнит (docs/03 §11).
    await quota_consumed(db, user, QuotaKind.PARSE)

    task_payload = {
        **payload.model_dump(exclude_none=True),
        **_blacklist_payload(payload),
    }
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
    # Валидация ссылки сразу на входе (docs/04 §4.2): 400 вместо падения воркера.
    search_url = validate_hh_search_url(payload.search_url)
    await quota_consumed(db, user, QuotaKind.PARSE)
    task_payload = {
        **payload.model_dump(exclude_none=True),
        "search_url": search_url,
        **_blacklist_payload(payload),
    }
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
    # Валидация ссылки сразу на входе (docs/04 §4.3): 400 вместо падения воркера.
    # Результат не используется — важен сам факт проверки формата; в payload
    # остаётся исходная ссылка, её разбирает воркер.
    extract_hh_vacancy_id(payload.vacancy_url)
    await quota_consumed(db, user, QuotaKind.PARSE)
    task_payload = payload.model_dump(exclude_none=True)
    task_id = await _create_task(db, user.id, "parse_manual", payload=task_payload)
    return ParseTaskResponse(task_id=str(task_id), status="pending")
