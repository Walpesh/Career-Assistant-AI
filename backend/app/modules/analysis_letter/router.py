"""Analysis & Letter Module — точки подключения роутеров (docs/03_API_CONTRACTS.md §6).

Контракт:
    POST /analysis/run            — запуск обработки:
                                     { vacancy_ids[], mode, match_threshold? }
                                     mode: analyze | letter | analyze_and_letter | auto
    GET  /analysis/{vacancy_id}   — получить анализ (match_score, strengths,
                                     weaknesses, summary — docs/02 §3.4)
    GET  /letters/{vacancy_id}    — получить сопроводительное письмо
                                     (content, version — docs/02 §3.5)

Логика режимов и промпты: docs/05_LLM_PIPELINE.md §2–6
(AUTO: анализ → проверка порога → письмо; LLM строго последовательно).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, Path
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.db.models import Analysis, CoverLetter, Task, Vacancy
from app.db.session import get_db
from app.modules.auth.deps import get_current_user

analysis_router = APIRouter(prefix="/analysis", tags=["analysis"])
letters_router = APIRouter(prefix="/letters", tags=["letters"])


# --- Schemas ---


class RunAnalysisRequest(BaseModel):
    """POST /analysis/run — запрос на запуск обработки (docs/03 §6)."""

    model_config = ConfigDict(from_attributes=True)

    vacancy_ids: list[uuid.UUID] = Field(..., description="Список ID вакансий для анализа")
    mode: Literal["analyze", "letter", "analyze_and_letter", "auto"] = Field(
        default="analyze", description="Режим обработки"
    )
    match_threshold: int | None = Field(
        default=None, ge=0, le=100, description="Порог матчинга (переопределяет профиль)"
    )


class AnalysisOut(BaseModel):
    """Ответ GET /analysis/{vacancy_id} — результат анализа (docs/02 §3.4)."""

    model_config = ConfigDict(from_attributes=True)

    vacancy_id: uuid.UUID
    match_score: int | None = None
    match_details: dict | None = None
    strengths: str | None = None
    weaknesses: str | None = None
    summary: str | None = None
    raw_llm_response: dict | None = None
    created_at: datetime
    updated_at: datetime


class LetterOut(BaseModel):
    """Ответ GET /letters/{vacancy_id} — сопроводительное письмо (docs/02 §3.5)."""

    model_config = ConfigDict(from_attributes=True)

    vacancy_id: uuid.UUID
    content: str
    version: int = 1
    raw_llm_response: dict | None = None
    created_at: datetime
    updated_at: datetime


class RunAnalysisResponse(BaseModel):
    """Ответ POST /analysis/run — { task_id, status: "pending" }."""

    task_id: str
    status: str = "pending"


# --- Endpoints ---

@analysis_router.post(
    "/run",
    response_model=RunAnalysisResponse,
    summary="Запуск обработки вакансий (docs/03 §6)",
)
async def run_analysis(
    payload: RunAnalysisRequest,
    user = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RunAnalysisResponse:
    """Запуск анализа и/или генерации письма для вакансий."""
    if not payload.vacancy_ids:
        raise AppError(400, "Не указаны вакансии для анализа", "INVALID_VACANCY_IDS")

    # Проверяем, что вакансии принадлежат пользователю
    for vid in payload.vacancy_ids:
        vacancy = await db.get(Vacancy, vid)
        if vacancy is None or vacancy.user_id != user.id:
            raise AppError(404, f"Вакансия {vid} не найдена", "NOT_FOUND")

    # Создаём задачу
    task_payload = payload.model_dump(exclude_none=True)
    task = Task(
        user_id=user.id,
        task_type="analyze",
        status="pending",
        progress_current=0,
        progress_total=len(payload.vacancy_ids),
        payload=task_payload,
    )
    db.add(task)
    await db.commit()
    await db.refresh(task)
    return RunAnalysisResponse(task_id=str(task.id), status="pending")


@analysis_router.get(
    "/{vacancy_id}",
    response_model=AnalysisOut,
    summary="Получить анализ вакансии (docs/03 §6)",
)
async def get_analysis(
    vacancy_id: uuid.UUID = Path(..., description="UUID вакансии"),
    user = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> AnalysisOut:
    """Получить анализ вакансии."""
    analysis = await db.scalar(
        select(Analysis).where(Analysis.vacancy_id == vacancy_id)
    )
    if analysis is None:
        raise AppError(404, "Анализ не найден", "NOT_FOUND")
    return analysis


# --- Letters Endpoints ---

@letters_router.get(
    "/{vacancy_id}",
    response_model=LetterOut,
    summary="Получить сопроводительное письмо (docs/03 §6)",
)
async def get_letter(
    vacancy_id: uuid.UUID = Path(..., description="UUID вакансии"),
    user = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> LetterOut:
    """Получить сопроводительное письмо."""
    letter = await db.scalar(
        select(CoverLetter).where(CoverLetter.vacancy_id == vacancy_id)
    )
    if letter is None:
        raise AppError(404, "Письмо не найдено", "NOT_FOUND")
    return letter
