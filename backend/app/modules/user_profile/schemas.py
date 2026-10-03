"""User Profile Module — Pydantic-схемы (docs/03_API_CONTRACTS.md §3, docs/02 §3.2).

PUT /profile поддерживает частичное обновление: все поля опциональны,
отсутствующие в теле запроса поля не изменяются (контрактный пример — docs/03 §3).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

__all__ = ["ProfileOut", "ProfileUpdate"]

# docs/02 §3.2: resume_text — «до 5000 символов».
RESUME_MAX_CHARS = 5000

# docs/02 §3.2: analysis_preferences и resume_addition — свободный текст.
ANALYSIS_PREFERENCES_MAX_CHARS = 2000
RESUME_ADDITION_MAX_CHARS = 2000


class ProfileOut(BaseModel):
    """Ответ GET/PUT /profile — строка user_profiles (docs/02 §3.2)."""

    model_config = ConfigDict(from_attributes=True)

    user_id: uuid.UUID
    full_name: str | None = None
    resume_text: str | None = None
    compact_resume: str | None = None
    skills: list[str] | None = None
    experience_years: float | None = None
    desired_salary_from: int | None = None
    desired_salary_to: int | None = None
    match_threshold: int
    preferred_work_formats: list[str] | None = None
    analysis_preferences: str | None = None
    resume_addition: str | None = None
    created_at: datetime
    updated_at: datetime

    @field_validator("experience_years", mode="before")
    @classmethod
    def _decimal_to_float(cls, value: object) -> object:
        """NUMERIC(4,1) → float для JSON (фронтенд сравнивает числа)."""
        if isinstance(value, Decimal):
            return float(value)
        return value


class ProfileUpdate(BaseModel):
    """Частичное обновление профиля — только присланные поля применяются."""

    full_name: str | None = Field(default=None, max_length=255)
    resume_text: str | None = Field(default=None, max_length=RESUME_MAX_CHARS)
    skills: list[str] | None = Field(default=None, max_length=50)
    experience_years: float | None = Field(default=None, ge=0, le=100)
    desired_salary_from: int | None = Field(default=None, ge=0)
    desired_salary_to: int | None = Field(default=None, ge=0)
    match_threshold: int | None = Field(default=None, ge=0, le=100)
    preferred_work_formats: list[str] | None = Field(default=None, max_length=10)
    analysis_preferences: str | None = Field(
        default=None,
        max_length=ANALYSIS_PREFERENCES_MAX_CHARS,
        description="Пожелания на человеческом языке: что не хочу видеть в вакансии",
    )
    resume_addition: str | None = Field(
        default=None,
        max_length=RESUME_ADDITION_MAX_CHARS,
        description="Текст, дописываемый в конец сопроводительного письма",
    )

    @field_validator("full_name", "resume_text")
    @classmethod
    def _strip_or_none(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None

    @field_validator("analysis_preferences", "resume_addition")
    @classmethod
    def _strip_text_or_none(cls, value: str | None) -> str | None:
        """Свободный текст профиля: обрезаем края, пустая строка = очистить поле."""
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None

    @field_validator("skills", "preferred_work_formats")
    @classmethod
    def _clean_list(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        return [item.strip() for item in value if item and item.strip()]

    @model_validator(mode="after")
    def _check_salary_range(self) -> "ProfileUpdate":
        if (
            self.desired_salary_from is not None
            and self.desired_salary_to is not None
            and self.desired_salary_from > self.desired_salary_to
        ):
            raise ValueError("desired_salary_from не может быть больше desired_salary_to")
        return self
