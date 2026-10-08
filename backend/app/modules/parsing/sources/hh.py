"""Адаптер источника hh.ru (docs/04_PARSING_RULES.md).

Выносит существующую hh-логику в отдельный адаптор БЕЗ изменений
поведения:

- fallback chain curl_cffi → Playwright + stealth (docs/04 §2) остаётся
  в ``fetcher.HhPageFetcher`` — адаптер его не дублирует;
- прогрев сессии, паузы 4–8 с, ротация резидентных прокси и anti-captcha
  (docs/04 §3) остаются в Proxy & Anti-Ban Module: адаптор получает
  готовую ``AntiBanSession`` (общую с ParsingOrchestrator);
- разбор выдачи (встроенный ``HH-Lux-InitialState`` + HTML-fallback) —
  ``listing.py``; URL-логика автопоиска/пагинации — ``urls.py``;
- разбор карточки — ``vacancy_storage.parser.extract_vacancy_fields``.

Все hh-специфичные функции модулей ``listing``/``urls`` инкапсулированы
здесь: оркестратор обращается к ним только через хуки адаптера.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from app.modules.anti_ban import AntiBanSession
from app.modules.parsing.fetcher import build_page_fetcher
from app.modules.parsing.listing import parse_listing
from app.modules.parsing.sources.base import BaseSourceAdapter
from app.modules.parsing.urls import (
    build_auto_search_url,
    build_page_url,
    validate_hh_search_url,
)
from app.modules.vacancy_storage.parser import extract_vacancy_fields
from app.modules.vacancy_storage.service import extract_hh_vacancy_id

__all__ = ["HHAdapter"]

#: Статусы «вакансия удалена на hh.ru» (docs/04 §5 → get_vacancy вернёт None).
_NOT_FOUND_STATUSES = (404, 410)


class HHAdapter(BaseSourceAdapter):
    """Источник hh.ru: выдача и карточки через AntiBanSession (docs/04 §2, §4)."""

    #: Имя источника в SourceRegistry и в vacancies.source по умолчанию.
    source_name = "hh"

    def __init__(self, session: AntiBanSession | None = None) -> None:
        # Сессия та же, что у оркестратора: единые прогрев/паузы/прокси и
        # общая статистика трафика (docs/04 §3). Свою создаёт только при
        # прямом использовании адаптера вне оркестратора.
        self.http = session or AntiBanSession(fetcher=build_page_fetcher())

    # --- контракт BaseSourceAdapter (docs/04 §10.1) --------------------------

    async def search(self, filters: dict) -> list[dict]:
        """Собрать карточки выдачи hh.ru с обходом пагинации (docs/04 §4.1–§4.2).

        Args:
            filters: ``search_url`` (готовая ссылка выдачи, §4.2) либо
                ``keywords``/``employment_forms``/``work_formats``/``schedules``
                (§4.1), а также ``max_pages`` (по умолчанию 5).

        Returns:
            list[dict]: сырые карточки ``{source, external_id, url}``
            в порядке выдачи, без дублей.
        """
        search_url = filters.get("search_url")
        max_pages = max(1, int(filters.get("max_pages") or 5))
        if search_url:
            base_url = self.validate_search_url(str(search_url))
        else:
            base_url = self.build_search_url(
                keywords=filters.get("keywords"),
                employment_forms=filters.get("employment_forms"),
                work_formats=filters.get("work_formats"),
                schedules=filters.get("schedules"),
            )

        results: list[dict] = []
        seen: set[str] = set()
        for page_index in range(max_pages):
            response = await self.http.fetch(self.page_url(base_url, page_index))
            vacancy_ids, has_next_page = self.parse_listing(response.text)
            for external_id in vacancy_ids:
                if external_id in seen:  # дедупликация внутри выдачи
                    continue
                seen.add(external_id)
                results.append(
                    {
                        "source": self.source_name,
                        "external_id": external_id,
                        "url": self.build_vacancy_url(base_url, external_id),
                    }
                )
            if not vacancy_ids or not has_next_page:
                break  # выдача или пагинация закончились
        return results

    async def get_vacancy(self, external_id_or_url: str) -> dict | None:
        """Карточка одной вакансии hh.ru по external_id или прямой ссылке.

        Returns:
            dict | None: канонические данные вакансии или None, если
            вакансия удалена на hh (docs/04 §5 → not_found).
        """
        url = self._resolve_vacancy_url(external_id_or_url)
        response = await self.http.fetch(url)
        if response.status_code in _NOT_FOUND_STATUSES:
            return None
        raw = {
            "source": self.source_name,
            "external_id": extract_hh_vacancy_id(url),
            "url": url,
            **extract_vacancy_fields(response.text),
        }
        return self.normalize(raw)

    def normalize(self, raw: dict) -> dict:
        """Сырые данные hh → каноническая схема вакансии (docs/04 §7)."""
        url = _as_text(raw.get("url"))
        external_id = _as_text(raw.get("external_id") or raw.get("hh_vacancy_id"))
        if not external_id and url and "hh.ru" in url:
            external_id = extract_hh_vacancy_id(url)
        return {
            "source": self.source_name,
            "external_id": external_id,
            "url": url,
            "title": _as_text(raw.get("title"), limit=512),
            "company_name": _as_text(raw.get("company_name"), limit=512),
            "salary_from": _as_int(raw.get("salary_from")),
            "salary_to": _as_int(raw.get("salary_to")),
            "salary_currency": _as_text(raw.get("salary_currency"), limit=8),
            "experience": _as_text(raw.get("experience"), limit=64),
            "employment_form": _as_text(raw.get("employment_form"), limit=64),
            "work_format": _as_text(raw.get("work_format"), limit=64),
            "schedule": _as_text(raw.get("schedule"), limit=128),
            "area": _as_text(raw.get("area"), limit=255),
            "published_at": raw.get("published_at"),
            "description_raw": raw.get("description_raw"),
            "description_html": raw.get("description_html"),
        }

    # --- хуки ParsingOrchestrator (docs/04 §10.2) ----------------------------

    @staticmethod
    def _resolve_city_area(city: str | None) -> int | None:
        """Привести название города к id территории hh.ru (area).

        Сопоставление берётся из https://github.com/hhru/api (любой ID
        региона). Если город не в карте или передан пустой ``city`` —
        возвращает ``None`` (без area-фильтра).
        """
        if not city:
            return None
        return _CITY_AREA_MAP.get(city.strip())

    def build_search_url(
        self,
        *,
        keywords: list[str] | None = None,
        employment_forms: list[str] | None = None,
        work_formats: list[str] | None = None,
        schedules: list[str] | None = None,
        page: int = 0,
        city: str | None = None,
    ) -> str:
        """Ссылка автопоиска hh.ru из ключевых слов и фильтров (docs/04 §4.1).

        Если ``city`` задан и найден в карте — к URL добавляется параметр
        ``area=<id региона>``, иначе параметр не добавляется (мирской поиск).
        """
        return build_auto_search_url(
            keywords=keywords,
            employment_forms=employment_forms,
            work_formats=work_formats,
            schedules=schedules,
            page=page,
            area=self._resolve_city_area(city),
        )

    def validate_search_url(self, search_url: str) -> str:
        """Принимаются только ссылки выдачи hh.ru (docs/04 §4.2)."""
        return validate_hh_search_url(search_url)

    def page_url(self, base_url: str, page: int) -> str:
        """Страница выдачи hh.ru (нумерация с нуля, docs/04 §4.1 п.2)."""
        return build_page_url(base_url, page)

    def parse_listing(self, html: str) -> tuple[list[str], bool]:
        """Карточки выдачи + признак пагинации (HH-Lux-InitialState → HTML)."""
        listing = parse_listing(html)
        return listing.vacancy_ids, listing.has_next_page

    def build_vacancy_url(self, base_url: str, external_id: str) -> str:
        """Прямая ссылка карточки: https://<хост выдачи>/vacancy/<id>."""
        host = urlsplit(base_url).netloc or "hh.ru"
        return f"https://{host}/vacancy/{external_id}"

    def extract_fields(self, html: str) -> dict:
        """Поля карточки hh.ru из HTML (docs/04 §7)."""
        return extract_vacancy_fields(html)

    # --- вспомогательное -----------------------------------------------------

    @staticmethod
    def _resolve_vacancy_url(external_id_or_url: str) -> str:
        """Прямая ссылка из external_id или переданного URL."""
        value = (external_id_or_url or "").strip()
        if value.startswith(("http://", "https://")):
            return value
        return f"https://hh.ru/vacancy/{value}"


def _as_text(value: object, *, limit: int | None = None) -> str | None:
    """Строковое значение: None/пусто → None, длинное — обрезается."""
    if value is None:
        return None
    result = str(value).strip()
    if not result:
        return None
    return result[:limit] if limit else result


def _as_int(value: object) -> int | None:
    """Целое значение зарплаты: нечисловое → None."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
