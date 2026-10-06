"""Абстрактный адаптер источника вакансий (docs/04_PARSING_RULES.md §10.1).

Источник (job board) — площадка, из которой собираются вакансии. Каждый
источник реализует контракт ``BaseSourceAdapter``; Parsing Orchestrator
работает только через этот контракт и SourceRegistry, поэтому подключение
новой площадки не требует правок оркестратора, очереди и API.

Обязательный контракт адаптера:

    source_name                  — уникальное имя источника в реестре;
    search(filters) -> list[dict] — сбор сырых карточек выдачи по фильтрам;
    get_vacancy(external_id_or_url) -> dict | None — одна вакансия
                                   (None — вакансия не найдена/удалена);
    normalize(raw) -> dict       — маппинг сырых данных в каноническую
                                   схему вакансии (docs/04 §7).

Каноническая схема (ключи результата ``normalize``)::

    source, external_id, url, title, company_name,
    salary_from, salary_to, salary_currency, experience,
    employment_form, work_format, schedule, area, published_at,
    description_raw, description_html

Anti-ban логика (curl_cffi → Playwright fallback, ротация прокси, прогрев
сессии, обработка капчи — docs/04 §2–§3) НЕ входит в контракт адаптера:
она остаётся в Proxy & Anti-Ban Module, адаптер получает готовую
``AntiBanSession`` и не дублирует эту логику.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

__all__ = ["BaseSourceAdapter"]


class BaseSourceAdapter(ABC):
    """Контракт адаптера источника вакансий (docs/04 §10.1)."""

    @property
    @abstractmethod
    def source_name(self) -> str:
        """Уникальное имя источника в реестре (например, «hh»)."""
        raise NotImplementedError

    @abstractmethod
    async def search(self, filters: dict) -> list[dict]:
        """Собрать сырые карточки выдачи по фильтрам источника.

        Args:
            filters: произвольный словарь фильтров источника (ключевые слова,
                формы занятости, лимит страниц, готовая ссылка выдачи и т.п.).

        Returns:
            list[dict]: сырые карточки; каждая содержит как минимум
            ``source`` и ``external_id``.
        """
        raise NotImplementedError

    @abstractmethod
    async def get_vacancy(self, external_id_or_url: str) -> dict | None:
        """Одна вакансия по внешнему id или прямой ссылке.

        Returns:
            dict | None: сырые данные вакансии либо None, если вакансия
            не найдена/удалена на источнике (docs/04 §5 → not_found).
        """
        raise NotImplementedError

    @abstractmethod
    def normalize(self, raw: dict) -> dict:
        """Привести сырые данные вакансии к канонической схеме (docs/04 §7)."""
        raise NotImplementedError

    # --- необязательные хуки ParsingOrchestrator (docs/04 §10.2) --------------
    #
    # Оркестратор сам обходит страницы выдачи и карточки: на нём лежат
    # прогресс (docs/04 §6), лимит max_pages (§4.1–§4.2), чёрный список
    # (§4.9) и сохранение в БД (§8). Для этого источнику нужны лёгкие хуки
    # URL-логики и разбора. Дефолты — NotImplementedError: источник обязан
    # переопределить их, используя свои ссылки и свои разборщики.

    def build_search_url(self, **filters) -> str:
        """Ссылка выдачи из фильтров автопоиска (docs/04 §4.1 п.1)."""
        raise NotImplementedError(f"{type(self).__name__} не поддерживает автопоиск")

    def validate_search_url(self, search_url: str) -> str:
        """Проверить ссылку выдачи пользователя (docs/04 §4.2).

        По умолчанию ссылка принимается без проверки; адаптеры источников
        с белым списком доменов переопределяют метод (как это делает hh).
        """
        return search_url

    def page_url(self, base_url: str, page: int) -> str:
        """Ссылка страницы пагинации выдачи (docs/04 §4.1 п.2, §4.2 п.2)."""
        raise NotImplementedError(f"{type(self).__name__} не поддерживает пагинацию")

    def parse_listing(self, html: str) -> tuple[list[str], bool]:
        """Разобрать страницу выдачи: (external_id в порядке выдачи, есть следующая)."""
        raise NotImplementedError(f"{type(self).__name__} не поддерживает разбор выдачи")

    def build_vacancy_url(self, base_url: str, external_id: str) -> str:
        """Прямая ссылка карточки вакансии по id и ссылке выдачи."""
        raise NotImplementedError(f"{type(self).__name__} не поддерживает карточки")

    def extract_fields(self, html: str) -> dict:
        """Извлечь поля вакансии из HTML карточки (docs/04 §7)."""
        raise NotImplementedError(f"{type(self).__name__} не поддерживает разбор карточек")
