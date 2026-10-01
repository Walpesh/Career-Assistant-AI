"""Pydantic-схемы Vacancy Storage Module (docs/03_API_CONTRACTS.md §4).

Формат списка — { items, total, page, size } (пагинация docs/03 §1: ?page&size);
статусы вакансий — docs/02_DATABASE.md §5: raw / analyzed / letter_ready /
applied / error; source — docs/02 §3.3: auto / group / manual.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Перечисления дублируют CHECK-ограничения app.db.models.Vacancy (docs/02 §3.3).
VacancyStatus = Literal["raw", "analyzed", "letter_ready", "applied", "error"]
VacancySource = Literal["auto", "group", "manual"]

__all__ = [
    "VacancyStatus",
    "VacancySource",
    "VacancyOut",
    "VacancyListOut",
    "ManualVacancyIn",
    "VacancyStatusUpdate",
]


class VacancyOut(BaseModel):
    """Карточка вакансии (таблица vacancies, docs/02 §3.3)."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    hh_vacancy_id: str
    url: str
    title: str | None = None
    company_name: str | None = None
    salary_from: int | None = None
    salary_to: int | None = None
    salary_currency: str | None = None
    experience: str | None = None
    employment_form: str | None = None
    work_format: str | None = None
    schedule: str | None = None
    area: str | None = None
    published_at: datetime | None = None
    description_raw: str | None = None
    description_html: str | None = None
    status: VacancyStatus
    match_score: int | None = None
    source: VacancySource | None = None
    created_at: datetime
    updated_at: datetime


class VacancyListOut(BaseModel):
    """Страница списка вакансий пользователя (GET /vacancies)."""

    items: list[VacancyOut]
    total: int
    page: int
    size: int


class ManualVacancyIn(BaseModel):
    """Тело POST /vacancies/manual — прямая ссылка на вакансию hh.ru."""

    vacancy_url: str = Field(
        ...,
        max_length=2048,
        description="Прямая ссылка вида https://<город>.hh.ru/vacancy/<id>",
        examples=["https://novokuznetsk.hh.ru/vacancy/137866214"],
    )

    @field_validator("vacancy_url")
    @classmethod
    def _strip_url(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("vacancy_url не может быть пустым")
        return value


class VacancyStatusUpdate(BaseModel):
    """Тело PATCH /vacancies/{vacancy_id}/status (docs/03 §4)."""

    status: VacancyStatus = Field(..., description="Новый статус вакансии (docs/02 §5)")
