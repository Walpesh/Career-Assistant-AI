"""Разбор страницы результатов поиска hh.ru (docs/04_PARSING_RULES.md §4.1–§4.2).

Задача модуля — вернуть список карточек выдачи (id + абсолютная ссылка), чтобы
оркестратор затем обошёл каждую детальную страницу (docs/04 §4.1 п.3).

Два независимых источника данных, как требует docs/04 §2:
    1. встроенный JSON `HH-Lux-InitialState` — основной (не зависит от вёрстки);
    2. HTML-разметка со ссылками `/vacancy/<id>` — резервный (docs/04 §5:
       «Изменение вёрстки → fallback»).

Лишняя информация (сайдбары, реклама, похожие вакансии) в разбор не попадает:
собираются только карточки выдачи.
"""

from __future__ import annotations

import json
import re
from html.parser import HTMLParser

__all__ = ["ListingPage", "parse_listing", "extract_listing_ids"]

#: Контейнер встроенного JSON hh.ru (docs/04 §2: «извлечение из HH-Lux-InitialState»).
_LUX_STATE_ID = "HH-Lux-InitialState"

_VACANCY_HREF_RELAXED = re.compile(r"/vacancy/(\d{1,12})", re.IGNORECASE)

#: Признак карточки именно в выдаче поиска (а не «похожие вакансии» в сайдбаре).
_SERP_ITEM_QAS = ("serp-item", "vacancy-serp__vacancy")


class ListingPage:
    """Результат разбора одной страницы выдачи."""

    __slots__ = ("vacancy_ids", "has_next_page")

    def __init__(self, vacancy_ids: list[str], *, has_next_page: bool = False) -> None:
        #: Уникальные hh_vacancy_id в порядке выдачи (без дублей).
        self.vacancy_ids = vacancy_ids
        #: Есть ли следующая страница (docs/04 §4.2 п.2 — обход пагинации).
        self.has_next_page = has_next_page

    def __len__(self) -> int:
        return len(self.vacancy_ids)

    def __repr__(self) -> str:  # pragma: no cover - диагностика
        return (
            f"ListingPage(vacancy_ids={len(self.vacancy_ids)}, "
            f"has_next_page={self.has_next_page})"
        )


class _LuxStateParser(HTMLParser):
    """Достаёт текст <script id="HH-Lux-InitialState">."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self._json: str | None = None
        self._inside = False
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "script":
            return
        attr = {key.lower(): (value or "") for key, value in attrs}
        if attr.get("id") == _LUX_STATE_ID:
            self._inside = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self._inside:
            self._inside = False

    def handle_data(self, data: str) -> None:
        if self._inside:
            self._parts.append(data)

    def result(self) -> str | None:
        if self._json is not None:
            return self._json
        self._json = "".join(self._parts).strip() or None
        return self._json


class _SerpParser(HTMLParser):
    """Собирает id карточек выдачи и признак наличия следующей страницы."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.vacancy_ids: list[str] = []
        self.has_next_page = False
        self._serp_depth = 0  # вложенность внутри карточки выдачи
        self._pending_href: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_map = {key.lower(): (value or "") for key, value in attrs}

        # Следующая страница пагинации hh.ru.
        data_qa = attr_map.get("data-qa", "")
        if "pager-next" in data_qa or "pager-next" in attr_map.get("class", ""):
            self.has_next_page = True

        is_serp_item = any(qa in data_qa for qa in _SERP_ITEM_QAS) or attr_map.get(
            "data-qh-position", ""
        ).startswith("vacancy-serp__")
        if is_serp_item:
            self._serp_depth += 1

        if self._serp_depth and tag == "a" and self._pending_href is None:
            self._pending_href = attr_map.get("href", "")

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._pending_href is not None:
            self._collect(self._pending_href)
            self._pending_href = None
        # Выход из карточки выдачи: счётчик вложенности уменьшаем на закрывающий тег.
        if tag in ("div", "article", "li") and self._serp_depth > 0:
            self._serp_depth -= 1

    def handle_data(self, data: str) -> None:
        # Кнопка «Далее» может быть без data-qa — текстовый признак.
        if self._serp_depth == 0 and data.strip().lower() in ("далее", "next", "»", "2"):
            self.has_next_page = True

    def _collect(self, href: str) -> None:
        match = _VACANCY_HREF_RELAXED.search(href)
        if match:
            self.vacancy_ids.append(match.group(1))

    def close_out(self) -> None:
        """Записать последнюю незакрытую ссылку (обрезанная/некорректная вёрстка)."""
        if self._pending_href is not None:
            self._collect(self._pending_href)
            self._pending_href = None


def _find_vacancy_list(node: object, depth: int = 0) -> list:
    """Найти список карточек по признаку vacancyId (в т.ч. внутри result)."""
    if depth > 6:
        return []
    if isinstance(node, dict):
        for key in ("vacancyList", "vacancies", "items"):
            value = node.get(key)
            if isinstance(value, list) and any(
                isinstance(item, dict) and ("vacancyId" in item or "vacancy_id" in item)
                for item in value
            ):
                return value
        for value in node.values():
            found = _find_vacancy_list(value, depth + 1)
            if found:
                return found
    elif isinstance(node, list):
        for item in node[:50]:
            found = _find_vacancy_list(item, depth + 1)
            if found:
                return found
    return []


def _ids_from_lux_state(html: str) -> list[str]:
    """Карточки из встроенного JSON hh.ru (docs/04 §2 — основной источник)."""
    parser = _LuxStateParser()
    parser.feed(html)
    parser.close()
    payload = parser.result()
    if not payload:
        return []

    try:
        data = json.loads(payload)
    except (ValueError, TypeError):
        return []  # формат изменился — уходим в HTML-резерв (docs/04 §5)
    if not isinstance(data, dict):
        return []

    vacancy_list = data.get("vacancyList") or data.get("vacancies")
    if not isinstance(vacancy_list, list) or not vacancy_list:
        vacancy_list = _find_vacancy_list(data)

    ids: list[str] = []
    for item in vacancy_list or []:
        if not isinstance(item, dict):
            continue
        raw = item.get("vacancyId") or item.get("vacancy_id") or item.get("id")
        if raw is None:
            continue
        value = str(raw).strip()
        if value:
            ids.append(value)
    return ids


def extract_listing_ids(html: str) -> list[str]:
    """Уникальные id вакансий страницы выдачи в порядке появления."""
    return _unique_preserve_order(_ids_from_lux_state(html) or _ids_from_markup(html))


def _ids_from_markup(html: str) -> list[str]:
    """Карточки из HTML-разметки (резервный источник, docs/04 §5)."""
    parser = _SerpParser()
    parser.feed(html)
    parser.close()
    parser.close_out()
    return parser.vacancy_ids


def _unique_preserve_order(ids: list[str]) -> list[str]:
    ordered: list[str] = []
    seen: set[str] = set()
    for vacancy_id in ids:
        if vacancy_id not in seen:
            seen.add(vacancy_id)
            ordered.append(vacancy_id)
    return ordered


def parse_listing(html: str) -> ListingPage:
    """Разобрать страницу выдачи: карточки + признак следующей страницы.

    Args:
        html: HTML страницы результатов поиска hh.ru.

    Returns:
        ListingPage: список id и признак наличия пагинации.
    """
    parser = _SerpParser()
    parser.feed(html)
    parser.close()
    parser.close_out()

    lux_ids = _ids_from_lux_state(html)
    if lux_ids:
        vacancy_ids = _unique_preserve_order(lux_ids)
        # Во встроенном JSON числа страниц нет, поэтому ориентируемся на
        # разметку пагинатора; если её нет — считаем, что страницы продолжаются
        # (docs/04 §4.2 п.2: обход страниц с лимитом max_pages).
        has_next_page = parser.has_next_page or bool(vacancy_ids)
    else:
        vacancy_ids = _unique_preserve_order(parser.vacancy_ids)
        has_next_page = parser.has_next_page or bool(vacancy_ids)

    return ListingPage(vacancy_ids, has_next_page=has_next_page)