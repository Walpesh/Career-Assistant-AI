"""Первичный raw-парсинг карточки вакансии hh.ru (docs/04_PARSING_RULES.md §4.3, §7).

Ручное добавление = один запрос на детальную страницу. Извлекается обязательный
минимум данных (docs/04 §7): заголовок, компания, зарплата, опыт, формат работы,
график, занятость, город, очищенное описание; сырой HTML сохраняется для отладки.

Сетевая часть выполняется через Proxy & Anti-Ban Module (``AntiBanSession``:
прогрев, human-паузы 4–8 с, ротация IP, retry — docs/04 §3, §5), а не прямым
httpx — POST /vacancies/manual обязан идти тем же защитным путём, что и
парсинг через воркер.

Ошибки:
    VacancyNotFound — на hh.ru ответ 404/410: вакансия удалена
                      (docs/04 §5 → статус vacancy = error);
    RawParseError   — сеть/статус/вёрстка не дали данных (капча, changed layout);
    CaptchaDetected / RateLimitExceeded — пробрасываются AntiBanSession
                      (docs/04 §5 → 503 в API, задачу/запрос нужно повторить).
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser

from app.modules.anti_ban import (
    AntiBanSession,
    CaptchaDetected,  # noqa: F401 — часть публичного контракта модуля
    ProxyError,
    RateLimitExceeded,  # noqa: F401 — часть публичного контракта модуля
)

__all__ = [
    "RawParseError",
    "VacancyNotFound",
    "fetch_raw_vacancy",
    "extract_vacancy_fields",
    "get_anti_ban_session",
]

#: Общая сессия обхода защит hh.ru: прогрев и cookies переиспользуются между
#: запросами ручного добавления (docs/04 §3.3).
_anti_ban_session: AntiBanSession | None = None


def get_anti_ban_session() -> AntiBanSession:
    """Ленивая AntiBanSession для первичного raw-парсинга (docs/04 §3).

    ``build_page_fetcher`` импортируется внутри функции: vacancy_storage и
    parsing зависят друг от друга, и импорт на уровне модуля замкнул бы цикл.
    """
    global _anti_ban_session
    if _anti_ban_session is None:
        from app.modules.parsing.fetcher import build_page_fetcher

        _anti_ban_session = AntiBanSession(fetcher=build_page_fetcher())
    return _anti_ban_session

# data-qa → модельные поля (best-effort: отсутствующие поля остаются None).
# Список содержит актуальные имена hh.ru и legacy-варианты: при смене вёрстки
# парсер продолжает собирать поля по одному из известных data-qa (docs/04 §5).
_QA_FIELDS: dict[str, tuple[str, ...]] = {
    "company_name": (
        "vacancy-company-name",
        "company-name",
        "employer-name",
    ),
    "description_raw": ("vacancy-description",),
    "experience": ("vacancy-experience", "work-experience-text"),
    # Актуальная разметка hh.ru отдаёт занятость как common-employment-text.
    "employment_form": ("vacancy-employment", "common-employment-text"),
    "work_format": (
        "vacancy-work_format-by-day",
        "work-format",
        "work-formats-text",
    ),
    "schedule": (
        "vacancy-schedule",
        "work-schedule-by-days-text",
        "working-hours-text",
    ),
    "area": (
        "vacancy-view-top-address",
        "vacancy-address",
        "vacancy-address-with-map",
        "vacancy-view-raw-address",
    ),
    "_salary_text": ("vacancy-salary", "vacancy-salary-raw"),
    # Дата публикации (docs/04 §7 — обязательное поле).
    "_published_text": (
        "vacancy-public-date",
        "vacancy-creation-date",
        "vacancy-published-date",
    ),
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
    Повторные вхождения того же поля игнорируются: hh.ru дублирует блоки
    (десктоп + мобильная вёрстка), иначе значение склеивалось бы дважды.
    """

    def __init__(self, qa_names: set[str]) -> None:
        super().__init__(convert_charrefs=True)
        self.meta: dict[str, str] = {}
        self.qa_text: dict[str, list[str]] = {name: [] for name in qa_names}
        self.title_parts: list[str] = []
        self._in_title = False
        self._capture_key: str | None = None
        self._capture_depth = 0
        self._captured: set[str] = set()
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
            if key is not None and key not in self._captured:
                self._capture_key = key
                self._capture_depth = 0
                self._captured.add(key)
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


def _parse_published_at(text: str | None) -> datetime | None:
    """Дата публикации из подписи карточки (docs/04 §7 — обязательное поле).

    hh.ru отдаёт «сегодня», «вчера», «3 августа» или дату вида «12.05.2025»;
    относительные значения пересчитываются от текущего дня.
    """
    if not text:
        return None
    value = text.strip().lower()
    today = datetime.now(timezone.utc).date()

    if "сегодня" in value:
        return datetime(today.year, today.month, today.day, tzinfo=timezone.utc)
    if "вчера" in value:
        moment = today - timedelta(days=1)
        return datetime(moment.year, moment.month, moment.day, tzinfo=timezone.utc)

    for pattern, has_year in (("%d.%m.%Y", True), ("%d.%m.%y", True)):
        match = re.search(r"\d{1,2}\.\d{1,2}\.\d{2,4}", value)
        if match:
            try:
                parsed = datetime.strptime(match.group(0), pattern)
            except ValueError:
                continue
            if has_year:
                return parsed.replace(tzinfo=timezone.utc)
    return None


def extract_vacancy_fields(html: str) -> dict[str, str | int | datetime | None]:
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
        "experience": _normalize_text(
            parser.qa_text.get("vacancy-experience", [])
            + parser.qa_text.get("work-experience-text", [])
        ),
        "employment_form": _normalize_text(
            parser.qa_text.get("vacancy-employment", [])
            + parser.qa_text.get("common-employment-text", [])
        ),
        "work_format": _normalize_text(
            parser.qa_text.get("vacancy-work_format-by-day", [])
            + parser.qa_text.get("work-format", [])
            + parser.qa_text.get("work-formats-text", [])
        ),
        "schedule": _normalize_text(
            parser.qa_text.get("vacancy-schedule", [])
            + parser.qa_text.get("work-schedule-by-days-text", [])
            + parser.qa_text.get("working-hours-text", [])
        ),
        "area": _normalize_text(
            parser.qa_text.get("vacancy-view-top-address", [])
            + parser.qa_text.get("vacancy-address", [])
            + parser.qa_text.get("vacancy-address-with-map", [])
            + parser.qa_text.get("vacancy-view-raw-address", [])
        ),
        # Дата публикации (docs/04 §7).
        "published_at": _parse_published_at(
            _normalize_text(
                parser.qa_text.get("vacancy-public-date", [])
                + parser.qa_text.get("vacancy-creation-date", [])
            )
        ),
        "description_raw": _normalize_text(parser.qa_text.get("vacancy-description", [])),
        # Сырой HTML для отладки (docs/04 §7, опционально).
        "description_html": html,
    }


async def fetch_raw_vacancy(url: str) -> dict:
    """GET карточки вакансии через AntiBanSession + извлечение полей (docs/04 §3, §5).

    Сессия сама обеспечивает прогрев homepage, human-паузы 4–8 с, ротацию IP,
    cookies и retry по таблице docs/04 §5 — POST /vacancies/manual больше не
    ходит в hh.ru прямым httpx.

    Поднимает:
        VacancyNotFound    — 404/410 → vacancy.status = 'error' (docs/04 §5);
        RawParseError      — неожиданный статус/не разобрана вёрстка;
        CaptchaDetected     — капча (docs/04 §2 п.3, §5) → 503 CAPTCHA_DETECTED;
        RateLimitExceeded  — 429 не отступил за попытки → 503 HH_RATE_LIMITED;
        ProxyError         — живых прокси нет → RawParseError.
    """
    session = get_anti_ban_session()
    try:
        response = await session.fetch(url)
    except ProxyError as exc:
        raise RawParseError(f"Запрос к hh.ru не удался: {exc}") from exc

    # 404/410 — вакансия удалена, повтор не поможет (docs/04 §5).
    if response.status_code in (404, 410):
        raise VacancyNotFound(
            f"Вакансия не найдена на hh.ru (HTTP {response.status_code})"
        )
    if response.status_code != 200:
        raise RawParseError(f"Неожиданный ответ hh.ru: HTTP {response.status_code}")

    fields = extract_vacancy_fields(response.text)
    if not fields.get("title"):
        # Капча или изменившаяся вёрстка: повтор не помогает (docs/04 §5).
        raise RawParseError(
            "Не удалось извлечь карточку вакансии (капча или изменилась вёрстка)"
        )
    return fields
