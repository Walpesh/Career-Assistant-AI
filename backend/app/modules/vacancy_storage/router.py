"""Vacancy Storage Module — endpoints (docs/03_API_CONTRACTS.md §4, §5 «manual»).

Контракт:
    GET    /vacancies                     — список с фильтрами:
             status (raw/analyzed/letter_ready/applied/error),
             source (auto/group/manual), search, min_match_score, page, size;
             ответ { items, total, page, size }
    POST   /vacancies/manual              — ручное добавление по прямой ссылке
             { vacancy_url } → извлекается hh_vacancy_id, выполняется
             первичный raw-парсинг (docs/04 §4.3) и сохранение со статусом
             raw / обновление существующей записи (201 создано / 200 обновлено)
    GET    /vacancies/{vacancy_id}        — детальная информация
    DELETE /vacancies/{vacancy_id}        — удалить вакансию
    PATCH  /vacancies/{vacancy_id}/status — смена статуса с проверкой графа

Особенности: уникальный ключ (user_id, hh_vacancy_id); правила дедупликации и
обновления — docs/04_PARSING_RULES.md §8; статусы — docs/02_DATABASE.md §5.
"""

from __future__ import annotations

import uuid
from typing import Literal

from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.db.models import User
from app.db.session import get_db
from app.modules.anti_ban import CaptchaDetected, RateLimitExceeded
from app.modules.auth.deps import get_current_user
from app.modules.vacancy_storage.parser import (
    RawParseError,
    VacancyNotFound,
    fetch_raw_vacancy,
)
from app.modules.vacancy_storage.schemas import (
    ManualVacancyIn,
    VacancyListOut,
    VacancyOut,
    VacancyStatusUpdate,
)
from app.modules.vacancy_storage.service import (
    change_status,
    delete_vacancy,
    extract_hh_vacancy_id,
    get_user_vacancy,
    list_user_vacancies,
    upsert_vacancy,
)

router = APIRouter(prefix="/vacancies", tags=["vacancies"])

_VacancyStatus = Literal["raw", "analyzed", "letter_ready", "applied", "error"]
_VacancySource = Literal["auto", "group", "manual"]


@router.get("", response_model=VacancyListOut, summary="Список вакансий пользователя")
async def list_vacancies(
    status_: _VacancyStatus | None = Query(
        None, alias="status", description="Фильтр по статусу (docs/02 §5)"
    ),
    source: _VacancySource | None = Query(None, description="Фильтр по источнику"),
    search: str | None = Query(
        None, max_length=255, description="Поиск по названию и компании"
    ),
    min_match_score: int | None = Query(None, ge=0, le=100),
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> VacancyListOut:
    """Пагинированный список вакансий текущего пользователя (docs/03 §4)."""
    items, total = await list_user_vacancies(
        db,
        user.id,
        status=status_,
        source=source,
        search=search,
        min_match_score=min_match_score,
        page=page,
        size=size,
    )
    return VacancyListOut(
        items=[VacancyOut.model_validate(item) for item in items],
        total=total,
        page=page,
        size=size,
    )


@router.post(
    "/manual",
    response_model=VacancyOut,
    status_code=status.HTTP_201_CREATED,
    summary="Добавить вакансию по прямой ссылке hh.ru",
)
async def manual_ingestion(
    payload: ManualVacancyIn,
    response: Response,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> VacancyOut:
    """Ручной ингест одной вакансии (docs/04 §4.3: один запрос на карточку).

    - валидация ссылки → 400 INVALID_VACANCY_URL;
    - первичный raw-парсинг: успех → статус raw, hh.ru 404/410 → error
      (docs/04 §5), сбой парсинга → 500 PARSING_FAILED (ничего не сохраняем);
    - дедупликация (user_id, hh_vacancy_id): создание → 201, обновление → 200;
      запись со статусом applied не изменяется (docs/04 §8).
    """
    hh_vacancy_id = extract_hh_vacancy_id(payload.vacancy_url)

    try:
        fields = await fetch_raw_vacancy(payload.vacancy_url)
        ingest_status = "raw"
    except VacancyNotFound:
        # docs/04 §5: «Вакансия реально удалена → error с причиной not_found».
        fields, ingest_status = {}, "error"
    except CaptchaDetected as exc:
        # docs/04 §2 п.3, §5: капчу нельзя обходить автоматически — просим
        # повторить запрос после ручного прохождения.
        raise AppError(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            f"Обнаружена капча hh.ru — повторите запрос позже: {exc}",
            "CAPTCHA_DETECTED",
        ) from exc
    except RateLimitExceeded as exc:
        raise AppError(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            f"Превышен лимит запросов hh.ru — повторите запрос позже: {exc}",
            "HH_RATE_LIMITED",
        ) from exc
    except RawParseError as exc:
        raise AppError(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            f"Не удалось разобрать страницу вакансии: {exc}",
            "PARSING_FAILED",
        ) from exc

    vacancy, created = await upsert_vacancy(
        db,
        user_id=user.id,
        hh_vacancy_id=hh_vacancy_id,
        url=payload.vacancy_url,
        source="manual",
        fields=fields,
        ingest_status=ingest_status,
    )
    if not created:
        response.status_code = status.HTTP_200_OK
    return VacancyOut.model_validate(vacancy)


@router.get("/{vacancy_id}", response_model=VacancyOut, summary="Вакансия (детально)")
async def get_vacancy(
    vacancy_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> VacancyOut:
    """Карточка вакансии; чужие и неизвестные id → 404 (docs/03 §4)."""
    vacancy = await get_user_vacancy(db, user.id, vacancy_id)
    return VacancyOut.model_validate(vacancy)


@router.delete(
    "/{vacancy_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Удалить вакансию",
)
async def remove_vacancy(
    vacancy_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Физическое удаление вместе со связанными анализом/письмом (docs/02 §4)."""
    vacancy = await get_user_vacancy(db, user.id, vacancy_id)
    await delete_vacancy(db, vacancy)


@router.patch(
    "/{vacancy_id}/status",
    response_model=VacancyOut,
    summary="Изменить статус вакансии",
)
async def update_vacancy_status(
    vacancy_id: uuid.UUID,
    payload: VacancyStatusUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> VacancyOut:
    """Смена статуса с проверкой жизненного цикла (docs/02 §5, docs/04 §8).

    Недопустимый переход (например, выход из applied) → 409
    INVALID_STATUS_TRANSITION; повтор текущего статуса → идемпотентный 200.
    """
    vacancy = await get_user_vacancy(db, user.id, vacancy_id)
    if change_status(vacancy, payload.status):
        await db.commit()
        await db.refresh(vacancy)
    return VacancyOut.model_validate(vacancy)

