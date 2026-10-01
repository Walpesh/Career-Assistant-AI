"""Первичный raw-парсинг карточки вакансии hh.ru (docs/04_PARSING_RULES.md §4.3, §7).

Ручное добавление = один запрос на детальную страницу. Извлекается обязательный
минимум данных (docs/04 §7): заголовок, компания, зарплата, опыт, формат работы,
график, занятость, город, очищенное описание; сырой HTML сохраняется для отладки.

Это быстрый путь (curl_cffi-интеграция и Playwright-fallback появятся в Parsing
Orchestrator / Proxy & Anti-Ban — docs/04 §2). Сейчас — httpx с браузерными
заголовками; разбор вёрстки терпим к отсутствию отдельных полей.

Ошибки:
    VacancyNotFound — на hh.ru ответ 404/410: вакансия удалена
                      (docs/04 §5 → статус vacancy = error);
    RawParseError   — сеть/статус/вёрстка не дали данных (капча, changed layout).
"""

from __future__ import annotations

import re
from html.parser import HTMLParser

import httpx

__all__ = [
    "RawParseError",
    "VacancyNotFound",
    "fetch_raw_vacancy",
    "extract_vacancy_fields",
]

# Минимальный набор браузерных заголовков (docs/04 §3.3 — полноценная маскировка
# живёт в Proxy & Anti-Ban Module; для одного запроса достаточно User-Agent).
_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
}

_DEFAULT_TIMEOUT = 15.0

# data-qa → модельные поля (best-effort: отсутствующие поля остаются None).
_QA_FIELDS: dict[str, tuple[str, ...]] = {
    "company_name": ("vacancy-company-name", "company-name", "employer-name"),
    "description_raw": ("vacancy-description",),
    "experience": ("vacancy-experience",),
    "employment_form": ("vacancy-employment",),
    "work_format": ("vacancy-work_format-by-day", "work-format"),
    "schedule": ("vacancy-schedule",),
    "area": ("vacancy-view-top-address", "vacancy-address"),
    "_salary_text": ("vacancy-salary", "vacancy-salary-raw"),
}

_BLOCK_TAGS = frozenset(
    {"p", "div", "li", "ul", "ol", "section", "article", "tr", "td", "th",
     "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "pre"}
)
_VOID_TAGS = frozenset({"br", "img", "hr", "input", "meta", "link", "source"})


class RawParseError(Exception):
    """Не удалось получить/разобрать карточку вакансии."""


class VacancyNotFound(RawParseError):
    """Вакансия отсутствует на hh.ru (HTTP 404/410) — docs/04 §5."""


class _CardParser(HTMLParser):
    """Собирает og:title/<title>, текст по data-qa и текст <title>.

    Захват по data-qa: на первом элементе с нужным атрибутом начинается сбор
    текста (с учётом вложенности), завершение — по закрывающему тегу элемента.
    """

    def __init__(self, qa_names: set[str]) -> None:
        super().__init__(convert_charrefs=True)
        self.meta: dict[str, str] = {}
        self.qa_text: dict[str, list[str]] = {name: [] for name in qa_names}
        self.title_parts: list[str] = []
        self._in_title = False
        self._capture_key: str | None = None
        self._capture_depth = 0
        self._qa_index: dict[str, str] = {name: name for name in qa_names}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = {k: (v or "") for k, v in attrs}

        if tag == "meta":
            prop = (attr.get("property") or attr.get("name") or "").lower()
            content = attr.get("content")
            if prop and content:
                self.meta.setdefault(prop, content)
            return

        if tag == "title":
            self._in_title = True
            return

        if self._capture_key is None:
            key = self._qa_index.get(attr.get("data-qa", ""))
            if key is not None:
                self._capture_key = key
                self._capture_depth = 0
                if tag in _VOID_TAGS:
                    self._capture_key = None
                return

        if self._capture_key is not None and tag not in _VOID_TAGS:
            self._capture_depth += 1
            if tag in _BLOCK_TAGS:
                self.qa_text[self._capture_key].append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
            return

        if self._capture_key is not None:
            if tag in _BLOCK_TAGS and tag not in _VOID_TAGS:
                self.qa_text[self._capture_key].append("\n")
            if self._capture_depth == 0:
                self._capture_key = None
            else:
                self._capture_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title_parts.append(data)
        if self._capture_key is not None:
            self.qa_text[self._capture_key].append(data)


def _normalize_text(parts: list[str]) -> str | None:
    """Склеивает куски текста, нормализуя переводы строк (очищенное описание)."""
    raw = "".join(parts)
    lines = [line.strip() for line in raw.splitlines()]
    text = "\n".join(line for line in lines if line)
    text = re.sub(r"[ \t\u00a0\u202f]+", " ", text).strip()
    return text or None


def _clean_title(value: str | None) -> str | None:
    """Убирает служебный суффикс «- / — / | hh.ru…» из og:title/<title>."""
    if not value:
        return None
    cleaned = re.sub(r"\s*[—–|-]\s*hh\.ru.*$", "", value, flags=re.IGNORECASE).strip()
    return cleaned or value.strip() or None


def _parse_salary(text: str | None) -> tuple[int | None, int | None, str | None]:
    """«от 120 000 ₽ до 150 000 ₽» → (120000, 150000, 'RUR')."""
    if not text:
        return None, None, None

    currency: str | None = None
    if "₽" in text or "руб" in text.lower():
        currency = "RUR"
    elif "$" in text:
        currency = "USD"
    elif "€" in text:
        currency = "EUR"

    values: list[int] = []
    for chunk in re.findall(r"\d[\d\u00a0\u202f ]*", text):
        digits = re.sub(r"\D", "", chunk)
        if digits:
            values.append(int(digits))
    if not values:
        return None, None, currency
    if len(values) >= 2:
        return values[0], values[1], currency

    # Одно число: «от X» → salary_from, «до X» → salary_to, иначе от.
    low = text.lower()
    if re.search(r"\bдо\b", low) and not re.search(r"\bот\b", low):
        return None, values[0], currency
    return values[0], None, currency


def extract_vacancy_fields(html: str) -> dict[str, str | int | None]:
    """Разбирает HTML карточки вакансии → поля модели Vacancy (docs/04 §7).

    Отсутствующие поля возвращаются как None; вызывающий код фильтрует None,
    чтобы не затирать уже сохранённые значения при повторной дедупликации.
    """
    parser = _CardParser({name for names in _QA_FIELDS.values() for name in names})
    parser.feed(html)
    parser.close()

    title = _clean_title(parser.meta.get("og:title")) or _clean_title(
        _normalize_text(parser.title_parts)
    )
    salary_from, salary_to, salary_currency = _parse_salary(
        _normalize_text(
            parser.qa_text.get("vacancy-salary", [])
            + parser.qa_text.get("vacancy-salary-raw", [])
        )
    )

    return {
        "title": title,
        "company_name": _normalize_text(
            parser.qa_text.get("vacancy-company-name", [])
            + parser.qa_text.get("company-name", [])
            + parser.qa_text.get("employer-name", [])
        ),
        "salary_from": salary_from,
        "salary_to": salary_to,
        "salary_currency": salary_currency,
        "experience": _normalize_text(parser.qa_text.get("vacancy-experience", [])),
        "employment_form": _normalize_text(parser.qa_text.get("vacancy-employment", [])),
        "work_format": _normalize_text(
            parser.qa_text.get("vacancy-work_format-by-day", [])
            + parser.qa_text.get("work-format", [])
        ),
        "schedule": _normalize_text(parser.qa_text.get("vacancy-schedule", [])),
        "area": _normalize_text(
            parser.qa_text.get("vacancy-view-top-address", [])
            + parser.qa_text.get("vacancy-address", [])
        ),
        "description_raw": _normalize_text(parser.qa_text.get("vacancy-description", [])),
        # Сырой HTML для отладки (docs/04 §7, опционально).
        "description_html": html,
    }


async def fetch_raw_vacancy(url: str, *, timeout: float = _DEFAULT_TIMEOUT) -> dict:
    """Один GET на детальную страницу + извлечение минимального набора данных.

    Поднимает VacancyNotFound (404/410 → vacancy.status = 'error', docs/04 §5)
    или RawParseError (сеть/неожиданный статус/не разобрана вёрстка).
    """
    try:
        async with httpx.AsyncClient(
            headers=_HEADERS, follow_redirects=True, timeout=timeout
        ) as client:
            response = await client.get(url)
    except httpx.HTTPError as exc:
        raise RawParseError(f"Запрос к hh.ru не удался: {exc}") from exc

    if response.status_code in (404, 410):
        raise VacancyNotFound(
            f"Вакансия не найдена на hh.ru (HTTP {response.status_code})"
        )
    if response.status_code != 200:
        raise RawParseError(f"Неожиданный ответ hh.ru: HTTP {response.status_code}")

    fields = extract_vacancy_fields(response.text)
    if not fields.get("title"):
        raise RawParseError(
            "Не удалось извлечь карточку вакансии (капча или изменилась вёрстка)"
        )
    return fields
