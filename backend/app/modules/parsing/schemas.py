"""Parsing Orchestrator — Pydantic-схемы запросов/ответов (docs/03_API_CONTRACTS.md §5).

Контракт:
    POST /parsing/auto   — Автопоиск
    POST /parsing/group  — Групповой парсер
    POST /parsing/manual — Ручное добавление
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from app.modules.parsing.service import MAX_BLACKLIST_WORDS

__all__ = [
    "ParseAutoRequest",
    "ParseGroupRequest",
    "ParseManualRequest",
    "ParseTaskResponse",
]


class ParseAutoRequest(BaseModel):
    """POST /parsing/auto — Автопоиск по ключевым словам (docs/03 §5).

    Фильтры комбинируются: можно одновременно передать любое количество
    форм занятости, форматов работы и графиков (docs/04 §4.1 п.1 — «URL на
    основе ключевых слов и фильтров»). Перечни значений — полный набор
    hh.ru, который использует интерфейс, поэтому выбор нескольких чипов
    больше не отсекается валидацией.
    """

    keywords: list[str] = Field(default_factory=list, description="Ключевые слова для поиска")
    employment_forms: list[
        Literal["full", "part", "project", "volunteer", "probation", "gph"]
    ] = Field(default_factory=list, description="Формы занятости")
    work_formats: list[Literal["remote", "hybrid", "onsite", "office"]] = Field(
        default_factory=list, description="Форматы работы"
    )
    schedules: list[
        Literal["fullDay", "flexible", "shift", "remote", "flyInFlyOut"]
    ] = Field(default_factory=list, description="Графики работы")
    match_threshold: int = Field(
        default=70, ge=0, le=100, description="Порог матчинга для автоматической генерации письма"
    )
    max_pages: int = Field(default=5, ge=1, le=20, description="Максимальное количество страниц результатов")
    city: Optional[str] = Field(
        default=None,
        description="Целевое название города для фильтрации вакансий (если пусто — поиск по всей России без городов)",
    )
    blacklist_enabled: bool = Field(
        default=False,
        description="Включён ли чёрный список слов (docs/04 §4.9). Выключен — фильтр не применяется",
    )
    blacklist_words: list[str] = Field(
        default_factory=list,
        max_length=MAX_BLACKLIST_WORDS,
        description="Слова, при найденном совпадении вакансия не сохраняется в БД",
    )

    model_config = {"json_schema_extra": {"example": {
        "keywords": ["python", "fastapi", "backend"],
        "employment_forms": ["full"],
        "work_formats": ["remote", "hybrid"],
        "schedules": ["fullDay"],
        "match_threshold": 75,
        "max_pages": 5,
        "blacklist_enabled": True,
        "blacklist_words": ["ТК РФ", "1С"]
    }}}


class ParseGroupRequest(BaseModel):
    """POST /parsing/group — Групповой парсер по готовой ссылке (docs/03 §5)."""

    search_url: str = Field(..., description="URL результатов поиска на hh.ru")
    max_pages: int = Field(default=5, ge=1, le=20, description="Максимальное количество страниц")
    blacklist_enabled: bool = Field(
        default=False,
        description="Включён ли чёрный список слов (docs/04 §4.9). Выключен — фильтр не применяется",
    )
    blacklist_words: list[str] = Field(
        default_factory=list,
        max_length=MAX_BLACKLIST_WORDS,
        description="Слова, при найденном совпадении вакансия не сохраняется в БД",
    )

    model_config = {"json_schema_extra": {"example": {
        "search_url": "https://novokuznetsk.hh.ru/vacancies/razrabotchik",
        "max_pages": 3,
        "blacklist_enabled": True,
        "blacklist_words": ["ТК РФ"]
    }}}


class ParseManualRequest(BaseModel):
    """POST /parsing/manual — Ручное добавление одной вакансии (docs/03 §5)."""

    vacancy_url: str = Field(..., description="Прямая ссылка на вакансию hh.ru")
    run_analysis: bool = Field(
        default=False, description="Запустить анализ после добавления"
    )

    model_config = {"json_schema_extra": {"example": {
        "vacancy_url": "https://novokuznetsk.hh.ru/vacancy/137866214",
        "run_analysis": True
    }}}


class ParseTaskResponse(BaseModel):
    """Ответ на запрос парсинга — { task_id, status: "pending" } (docs/03 §5)."""

    task_id: str = Field(
        description="UUID задачи в формате строки (для совместимости с JSON)"
    )
    status: Literal["pending"] = Field(
        default="pending", description="Текущий статус задачи"
    )

    model_config = {"json_schema_extra": {"example": {
        "task_id": "550e8400-e29b-41d4-a716-446655440000",
        "status": "pending"
    }}}