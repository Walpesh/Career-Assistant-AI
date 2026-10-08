"""Формирование URL результатов поиска hh.ru (docs/04_PARSING_RULES.md §4.1, §4.2).

Автопоиск (§4.1) строит ссылку из ключевых слов и фильтров; групповой парсер
(§4.2) получает готовую ссылку от пользователя и только достраивает пагинацию.

Валидация: принимаются только ссылки на hh.ru — иначе AppError 400
(согласуется с контрактом ручного добавления, docs/04 §4.3).
"""

from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from app.core.errors import AppError

__all__ = [
    "HH_SEARCH_BASE",
    "build_auto_search_url",
    "build_page_url",
    "validate_hh_search_url",
]

#: Базовая ссылка результатов поиска hh.ru (docs/04 §4.1 п.1).
HH_SEARCH_BASE = "https://hh.ru/search/vacancy"

_HH_HOST_SUFFIX = "hh.ru"

#: Параметры пагинации hh.ru: страницы нумеруются с нуля (?page=0).
_PAGE_PARAM = "page"


def _clean_host(hostname: str | None) -> str:
    return (hostname or "").lower()


def validate_hh_search_url(search_url: str) -> str:
    """Проверить, что ссылка ведёт на hh.ru; иначе 400 INVALID_SEARCH_URL.

    docs/04 §4.2: пользователь передаёт готовую ссылку результатов поиска —
    внешние домены отклоняются, чтобы парсер не уходил за пределы hh.ru.
    """
    candidate = (search_url or "").strip()
    parts = urlsplit(candidate)
    host = _clean_host(parts.hostname)
    if parts.scheme not in ("http", "https") or not host:
        raise AppError(
            400,
            "Ожидается ссылка на результаты поиска hh.ru вида https://hh.ru/search/vacancy?...",
            "INVALID_SEARCH_URL",
        )
    if host != _HH_HOST_SUFFIX and not host.endswith("." + _HH_HOST_SUFFIX):
        raise AppError(
            400,
            f"Домен «{parts.hostname}» не является поддоменом hh.ru",
            "INVALID_SEARCH_URL",
        )
    return candidate


def build_auto_search_url(
    *,
    keywords: list[str] | None = None,
    employment_forms: list[str] | None = None,
    work_formats: list[str] | None = None,
    schedules: list[str] | None = None,
    page: int = 0,
    area: str | None = None,
) -> str:
    """Ссылка на результаты поиска по ключевым словам и фильтрам (docs/04 §4.1).

    Ключевые слова объединяются в один текстовый запрос (hh.ru ищет по «text»),
    фильтры пробрасываются штатными параметрами поиска. Пустые фильтры в URL не
    попадают, чтобы ссылка оставалась читаемой и повторяемой.

    Args:
        keywords: Ключевые слова запроса.
        employment_forms: Формы занятости.
        work_formats: Форматы работы.
        schedules: Графики работы.
        page: Номер страницы (нумерация с нуля).
        area: ID территории hh.ru (число) для локализации поиска.
    """
    text = " ".join(word.strip() for word in (keywords or []) if word and word.strip())
    params: list[tuple[str, str]] = [("text", text)] if text else []
    if employment_forms:
        params.append(("employment", ",".join(employment_forms)))
    if work_formats:
        params.append(("work_format", ",".join(work_formats)))
    if schedules:
        params.append(("schedule", ",".join(schedules)))
    if area:
        params.append(("area", str(area)))
    params.append((_PAGE_PARAM, str(max(0, page))))
    return f"{HH_SEARCH_BASE}?{urlencode(params)}"


def build_page_url(base_url: str, page: int) -> str:
    """Ссылка на конкретную страницу выдачи (docs/04 §4.2 п.2 — обход пагинации).

    Сохраняет все существующие параметры ссылки (text, area, фильтры) и
    заменяет только номер страницы. Нумерация hh.ru начинается с нуля.
    """
    parts = urlsplit(base_url)
    query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key != _PAGE_PARAM
    ]
    query.append((_PAGE_PARAM, str(max(0, page))))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))