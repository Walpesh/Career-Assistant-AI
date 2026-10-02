"""Analysis & Letter Module — исполнение задач обработки вакансий.

Модуль реализует стадии docs/05_LLM_PIPELINE.md:
    §4  Этап 1 — анализ вакансии и расчёт match_score;
    §5  Этап 2 — генерация сопроводительного письма;
    §6  Логика режима AUTO (анализ → проверка порога → письмо);
    §9  Версионирование писем (cover_letters.version).

Режимы (docs/05 §2): analyze | letter | analyze_and_letter | auto.

Вызовы LLM строго последовательные (docs/05 §1) — единственный LLM-воркер
Queue Manager (docs/04 §6) обрабатывает такие задачи по одной.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Analysis, CoverLetter, UserProfile, Vacancy
from app.modules.analysis_letter.llm import (
    LLMError,
    analyze_vacancy,
    generate_cover_letter,
)

__all__ = ["ProcessingOutcome", "process_vacancy", "MODE_BY_TASK_TYPE"]

logger = logging.getLogger(__name__)

#: Тип задачи очереди → режим обработки (docs/02 §3.6, docs/05 §2).
MODE_BY_TASK_TYPE: dict[str, str] = {
    "analyze": "analyze",
    "generate_letter": "letter",
    "auto_full": "auto",
}


@dataclass
class ProcessingOutcome:
    """Итог обработки одной вакансии (пишется в tasks.result)."""

    vacancy_id: str
    analyzed: bool = False
    match_score: int | None = None
    letter_generated: bool = False
    letter_version: int = 0
    status: str = "raw"
    skipped_reason: str | None = None
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "vacancy_id": self.vacancy_id,
            "analyzed": self.analyzed,
            "match_score": self.match_score,
            "letter_generated": self.letter_generated,
            "letter_version": self.letter_version,
            "status": self.status,
        }
        if self.skipped_reason:
            data["skipped"] = self.skipped_reason
        if self.errors:
            data["errors"] = self.errors
        return data


def _vacancy_fields(vacancy: Vacancy, profile: UserProfile | None) -> dict[str, Any]:
    """Данные вакансии для промпта + контекст профиля (docs/05 §1)."""
    fields: dict[str, Any] = {
        "title": vacancy.title,
        "company_name": vacancy.company_name,
        "salary_from": vacancy.salary_from,
        "salary_to": vacancy.salary_to,
        "experience": vacancy.experience,
        "employment_form": vacancy.employment_form,
        "work_format": vacancy.work_format,
        "schedule": vacancy.schedule,
        "area": vacancy.area,
        "description_raw": vacancy.description_raw,
    }
    if profile is not None:
        fields["skills"] = profile.skills
        fields["experience_years"] = float(profile.experience_years) if profile.experience_years is not None else None
    return fields


def _list_to_text(items: list[str]) -> str:
    """Список пунктов модели → текст для колонок TEXT (docs/02 §3.4)."""
    return "\n".join(f"• {item}" for item in items)


async def _save_analysis(
    db: AsyncSession, vacancy: Vacancy, result
) -> Analysis:
    """Upsert анализа вакансии: одна запись на вакансию (docs/02 §3.4)."""
    analysis = await db.scalar(select(Analysis).where(Analysis.vacancy_id == vacancy.id))
    if analysis is None:
        analysis = Analysis(vacancy_id=vacancy.id)
        db.add(analysis)
    analysis.match_score = result.match_score
    analysis.match_details = result.match_details
    analysis.strengths = _list_to_text(result.strengths)
    analysis.weaknesses = _list_to_text(result.weaknesses)
    analysis.summary = result.summary
    analysis.raw_llm_response = result.raw

    # match_score дублируется в vacancies для фильтра min_match_score (docs/03 §4).
    vacancy.match_score = result.match_score
    return analysis


async def _save_letter(
    db: AsyncSession, vacancy: Vacancy, content: str, raw: dict[str, Any]
) -> CoverLetter:
    """Письмо с версионированием: новая запись, version = previous + 1 (docs/05 §9)."""
    existing = await db.scalar(select(CoverLetter).where(CoverLetter.vacancy_id == vacancy.id))
    if existing is None:
        letter = CoverLetter(vacancy_id=vacancy.id, content=content, version=1, raw_llm_response=raw)
        db.add(letter)
        return letter

    existing.version = int(existing.version or 1) + 1
    existing.content = content
    existing.raw_llm_response = raw
    return existing
async def process_vacancy(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    vacancy_id: uuid.UUID,
    mode: str,
    match_threshold: int | None = None,
    progress=None,
) -> ProcessingOutcome:
    """Обработать одну вакансию в заданном режиме (docs/05 §2–§6).

    Порог: из запроса, иначе из профиля пользователя (docs/05 §6 п.3).
    В режиме auto письмо генерируется только при match_score >= порога.
    """
    outcome = ProcessingOutcome(vacancy_id=str(vacancy_id))

    vacancy = await db.get(Vacancy, vacancy_id)
    if vacancy is None or vacancy.user_id != user_id:
        raise LLMError(f"Вакансия {vacancy_id} не найдена")
    outcome.status = vacancy.status

    profile = await db.get(UserProfile, user_id)
    compact_resume = (profile.compact_resume if profile else None) or ""
    if not compact_resume.strip():
        raise LLMError(
            "Пустое compact_resume: сначала выполните POST /profile/convert-resume"
        )

    threshold = (
        match_threshold
        if match_threshold is not None
        else int(profile.match_threshold if profile else 70)
    )
    fields = _vacancy_fields(vacancy, profile)

    need_analysis = mode in ("analyze", "analyze_and_letter", "auto")
    need_letter = mode in ("letter", "analyze_and_letter")

    if need_analysis:
        if progress is not None:
            await progress("Анализ вакансии: LLM формирует match_score")
        try:
            result = await analyze_vacancy(compact_resume, fields)
        except LLMError as exc:
            outcome.errors.append(f"анализ: {exc}")
            await db.commit()
            if vacancy.status not in ("applied", "error"):
                vacancy.status = "error"
                await db.commit()
            outcome.status = vacancy.status
            return outcome

        await _save_analysis(db, vacancy, result)
        await db.commit()
        await db.refresh(vacancy)
        outcome.analyzed = True
        outcome.match_score = result.match_score

        if vacancy.status != "applied":
            vacancy.status = "analyzed"
            await db.commit()
            await db.refresh(vacancy)
        outcome.status = vacancy.status

        if mode == "auto" and result.match_score < threshold:
            # docs/05 §6 п.5: письмо не генерируется, статус остаётся analyzed.
            outcome.skipped_reason = (
                f"match_score {result.match_score} < порога {threshold}"
            )
            logger.info(
                "AUTO: письмо не сгенерировано для %s (%s)", vacancy.hh_vacancy_id, outcome.skipped_reason
            )
            return outcome

    if need_letter or (need_analysis and mode == "auto"):
        if progress is not None:
            await progress("Генерация сопроводительного письма: LLM пишет текст")
        try:
            content = await generate_cover_letter(compact_resume, fields)
        except LLMError as exc:
            outcome.errors.append(f"письмо: {exc}")
            await db.commit()
            if not outcome.analyzed and vacancy.status != "applied":
                vacancy.status = "error"
                await db.commit()
                await db.refresh(vacancy)
            outcome.status = vacancy.status
            return outcome

        letter = await _save_letter(db, vacancy, content, {"length": len(content)})
        await db.commit()
        await db.refresh(vacancy)
        outcome.letter_generated = True
        outcome.letter_version = int(letter.version)

        if vacancy.status != "applied":
            vacancy.status = "letter_ready"
            await db.commit()
            await db.refresh(vacancy)
        outcome.status = vacancy.status

    return outcome