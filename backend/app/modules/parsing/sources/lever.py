"""Lever Job Board source adapter (docs/04_PARSING_RULES.md §10).

Площадка — публичные доски вакансий Lever (``site``/слаг компании,
например ``rover`` в ``jobs.lever.co/rover``). Два пути получения данных:

- **Основной** — публичный Postings API
  ``https://api.lever.co/v0/postings/{site}?mode=json`` (без ключа): ответ —
  JSON-массив объектов вакансий с полями ``id`` (UUID), ``text`` (заголовок),
  ``hostedUrl``/``applyUrl``, ``categories`` (``location``, ``commitment``,
  ``team``, ``department``), ``createdAt`` (epoch мс), ``description`` (HTML),
  ``descriptionPlain``, ``lists``, ``workplaceType``, ``country`` и опционально
  ``salaryRange`` (``min``/``max``/``currency``). Одна вакансия —
  ``.../postings/{site}/{id}``. Запрос идёт через общую ``AntiBanSession``
  оркестратора.
- **Fallback** — при отказе/непригодности API (403/5xx, не-JSON, сетевая
  ошибка, доска отключила публичный API) HTML-разбор страницы выдачи
  ``https://jobs.lever.co/{site}`` (разметка ``div.posting`` с
  ``a.posting-title`` → ``h5`` + ``posting-categories``; заголовок карточки —
  ``.posting-headline h2``, описание — секция ``data-qa="job-description"``).

Доска задаётся явно: ``site`` в конструкторе либо в фильтрах ``search`` /
``build_search_url`` (``site``, ``company``, ``search_url``). Без явного слага
используется пресет целевых tech-компаний ``PRESET_SITES`` (первый элемент —
доска по умолчанию). EU-инстанс Lever (``api.eu.lever.co`` /
``jobs.eu.lever.co``) поддерживается через ``region`` и определяется из
ссылок выдачи/карточек.

Нормализация (``normalize``) приводит сырые данные к канонической схеме
docs/04 §7 (ключи ``source``, ``external_id``, ``url``, ``title``,
``company_name``, ``salary_from/to/currency``, ``experience``,
``employment_form``, ``work_format``, ``schedule``, ``area``,
``published_at``, ``description_raw``, ``description_html``) с
``source="lever"``; ``createdAt`` (epoch мс) конвертируется в ISO-8601,
``workplaceType`` (remote/hybrid/on-site) — в ``work_format``.

Anti-ban (прогрев, паузы 4–8 с, ротация прокси) остаётся в Proxy & Anti-Ban
Module: API и страницы запрашиваются через готовую ``AntiBanSession``.
"""

from __future__ import annotations

import html as html_lib
import json
import logging
import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

from app.modules.anti_ban import AntiBanSession
from app.modules.parsing.fetcher import build_page_fetcher
from app.modules.parsing.sources.base import BaseSourceAdapter

__all__ = [
    "API_BASE",
    "EU_API_BASE",
    "LISTING_BASE",
    "EU_LISTING_BASE",
    "PRESET_SITES",
    "PRESET_COMPANY_NAMES",
    "LeverAdapter",
    "LeverAPIError",
    "api_url_for",
    "api_posting_url_for",
    "company_name_for",
    "extract_card_fields",
    "has_next_page",
    "job_url_for",
    "listing_url_for",
    "normalize_site",
    "parse_api_postings",
    "parse_html_postings",
]

logger = logging.getLogger(__name__)

#: Публичный Postings API Lever (без ключа).
API_BASE = "https://api.lever.co/v0/postings"
#: EU-инстанс Lever (data-residency европейских досок).
EU_API_BASE = "https://api.eu.lever.co/v0/postings"
#: Страницы выдачи доски — цель HTML-fallback при отказе API.
LISTING_BASE = "https://jobs.lever.co"
#: EU-инстанс страниц выдачи.
EU_LISTING_BASE = "https://jobs.eu.lever.co"

#: Пресет целевых tech-компаний: используется, когда ``site`` не задан.
PRESET_SITES: tuple[str, ...] = (
    "rover",
    "ramp",
    "coupa",
    "aircall",
    "zoox",
)

#: Отображаемое имя компании для слагов пресета (фолбэк company_name).
PRESET_COMPANY_NAMES: dict[str, str] = {
    "rover": "Rover",
    "ramp": "Ramp",
    "coupa": "Coupa",
    "aircall": "Aircall",
    "zoox": "Zoox",
}

#: Разрешённые хосты ссылок API/выдачи/карточек (docs/04 §4.2 — белый список).
_ALLOWED_HOSTS = frozenset(
    {
        "api.lever.co",
        "api.eu.lever.co",
        "jobs.lever.co",
        "jobs.eu.lever.co",
        "www.jobs.lever.co",
        "www.jobs.eu.lever.co",
    }
)
#: Статусы «вакансия удалена» (docs/04 §5 → get_vacancy вернёт None).
_NOT_FOUND_STATUSES = (404, 410)

#: UUID вакансии Lever (внешний id).
_UUID_PATTERN = (
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
_UUID_RE = re.compile(_UUID_PATTERN)

#: Ссылка карточки в выдаче: ``a.posting-title`` с href (порядок атрибутов
#: не важен — class и href ловятся через look-ahead).
_TITLE_ANCHOR_RE = re.compile(
    r'<a\b(?=[^>]*\bclass="[^"]*\bposting-title\b[^"]*")'
    r'(?=[^>]*\bhref="(?P<href>[^"]+)")[^>]*>(?P<inner>.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)
#: Заголовок в выдаче: ``<h5 data-qa="posting-name">Title</h5>``.
_H5_RE = re.compile(r"<h5\b[^>]*>(?P<text>.*?)</h5>", re.IGNORECASE | re.DOTALL)
#: Заголовок карточки: ``<h2>`` внутри ``.posting-headline``.
_H2_RE = re.compile(r"<h2\b[^>]*>(?P<text>.*?)</h2>", re.IGNORECASE | re.DOTALL)
#: Заголовок карточки: первый ``<h1>`` (запасной вариант).
_H1_RE = re.compile(r"<h1\b[^>]*>(?P<text>.*?)</h1>", re.IGNORECASE | re.DOTALL)
#: Шапка группы в выдаче: ``<div class="large-category-header">Team</div>``.
_GROUP_HEADER_RE = re.compile(
    r'<div\b[^>]*class="[^"]*\blarge-category-header\b[^"]*"[^>]*>'
    r"(?P<text>.*?)</div>",
    re.IGNORECASE | re.DOTALL,
)
#: Заголовок страницы: ``<title>…</title>`` (выдача — компания, карточка —
#: «Company - Title»).
_TITLE_RE = re.compile(r"<title[^>]*>(?P<text>.*?)</title>", re.IGNORECASE | re.DOTALL)
#: Мета-теги карточки.
_OG_TITLE_RE = re.compile(
    r'<meta[^>]*property=["\']og:title["\'][^>]*content=["\'](?P<text>[^"\']*)["\']',
    re.IGNORECASE,
)
_OG_DESCRIPTION_RE = re.compile(
    r'<meta[^>]*property=["\']og:description["\'][^>]*content=["\'](?P<text>[^"\']*)["\']',
    re.IGNORECASE,
)
#: Описание карточки: секция ``<div ... data-qa="job-description">``.
_JOB_DESC_OPEN_RE = re.compile(r'<div\b[^>]*data-qa="job-description"', re.IGNORECASE)
#: Пагинация: ссылки ``?page=N`` (у Lever-досок отсутствуют — фолбэк-индикатор).
_PAGE_LINK_RE = re.compile(r'href=["\'][^"\']*[?&]page=\d+', re.IGNORECASE)


class LeverAPIError(RuntimeError):
    """Ошибка Postings API Lever (не-JSON, 4xx/5xx, сетевой сбой)."""


# --- URL и вспомогательные функции -------------------------------------------


def normalize_site(value: object) -> str | None:
    """Нормализовать ``site`` (слаг компании): обрезка, слэши, валидация.

    Returns:
        str | None: чистый слаг либо None для пустого значения.

    Raises:
        ValueError: недопустимые символы (слаг участвует в построении URL).
    """
    if value is None:
        return None
    site = str(value).strip().strip("/")
    if not site:
        return None
    if not re.fullmatch(r"[A-Za-z0-9_\-]+", site):
        raise ValueError(f"Некорректный Lever site slug: {value!r}")
    return site


def _api_base(region: str | None = None) -> str:
    """База Postings API для региона (``eu`` → EU-инстанс)."""
    return EU_API_BASE if (region or "").lower() == "eu" else API_BASE


def _listing_base(region: str | None = None) -> str:
    """База страниц выдачи для региона (``eu`` → EU-инстанс)."""
    return EU_LISTING_BASE if (region or "").lower() == "eu" else LISTING_BASE


def api_url_for(site: str, region: str | None = None) -> str:
    """Ссылка Postings API доски (``mode=json`` — JSON-массив вакансий)."""
    return f"{_api_base(region)}/{site}?mode=json"


def api_posting_url_for(site: str, external_id: str, region: str | None = None) -> str:
    """Ссылка одной вакансии Postings API (``.../postings/{site}/{id}``)."""
    return f"{_api_base(region)}/{site}/{external_id}"


def listing_url_for(site: str, region: str | None = None) -> str:
    """Страница выдачи доски — цель HTML-fallback."""
    return f"{_listing_base(region)}/{site}"


def job_url_for(site: str, external_id: str, region: str | None = None) -> str:
    """Прямая ссылка карточки вакансии доски."""
    return f"{_listing_base(region)}/{site}/{external_id}"


def company_name_for(site: str | None) -> str | None:
    """Отображаемое имя компании для слага пресета (иначе None)."""
    if not site:
        return None
    return PRESET_COMPANY_NAMES.get(site)


def _is_api_url(url: str) -> bool:
    """Правда, если ссылка указывает на Postings API Lever."""
    host = (urlsplit(url or "").hostname or "").lower()
    return host in ("api.lever.co", "api.eu.lever.co")


def _region_from_url(url: object) -> str | None:
    """``eu`` для EU-хостов Lever, иначе None (глобальный инстанс)."""
    host = (urlsplit(_as_text(url) or "").hostname or "").lower()
    return "eu" if ".eu." in host else None


def _site_from_url(url: object) -> str | None:
    """``site`` из ссылки API/выдачи/карточки (иначе None)."""
    value = _as_text(url)
    if not value or "lever.co" not in value:
        return None
    parts = [part for part in urlsplit(value).path.split("/") if part]
    if not parts:
        return None
    if "postings" in parts:  # /v0/postings/<site>[/<id>]
        index = parts.index("postings")
        candidate = parts[index + 1] if index + 1 < len(parts) else None
    else:  # jobs.lever.co/<site>[/<id>]
        candidate = parts[0]
    try:
        return normalize_site(candidate)
    except ValueError:
        return None


def _as_text(value: object, *, limit: int | None = None) -> str | None:
    """Строковое значение: None/пусто → None, длинное — обрезается."""
    if value is None:
        return None
    result = str(value).strip()
    if not result:
        return None
    return result[:limit] if limit else result


def _strip_tags(fragment: str) -> str | None:
    """HTML → плоский текст (сущности раскрываются, пробелы схлопываются)."""
    text = re.sub(r"<[^>]+>", " ", fragment)
    text = html_lib.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text or None


def _clean_label(fragment: str | None) -> str | None:
    """Метка категории вида «Full-time /» или «Hybrid — » → «Full-time»."""
    text = _strip_tags(fragment or "")
    if not text:
        return None
    return text.strip(" \t\r\n/—–-") or None


def _uuid_from(value: object) -> str | None:
    """UUID вакансии из ссылки/строки (иначе None)."""
    match = _UUID_RE.search(_as_text(value) or "")
    return match.group(0) if match else None


def _category_value(fragment: str, token: str) -> str | None:
    """Текст категории с классом ``token`` (location/commitment/workplaceTypes).

    Разметка выдачи (``span``) и карточки (``div``) у Lever разная, но в
    обоих случаях класс заканчивается токеном категории.
    """
    pattern = re.compile(
        r'<(?:span|div)\b[^>]*class="[^"]*\b' + re.escape(token) + r'\b[^"]*"[^>]*>'
        r"(?P<text>.*?)</(?:span|div)>",
        re.IGNORECASE | re.DOTALL,
    )
    match = pattern.search(fragment)
    if not match:
        return None
    return _clean_label(match.group("text"))


def _body_region(html: str) -> str:
    """Хвост документа от ``<body>`` (вне CSS: классы встречаются и в стилях)."""
    index = html.lower().find("<body")
    return html[index:] if index >= 0 else html


def _extract_balanced_at(html: str, start: int, opening_re: re.Pattern[str]) -> str | None:
    """Извлечь содержимое ``<div>``, открывающегося не раньше ``start``."""
    if start < 0:
        return None
    match = opening_re.search(html, start)
    if not match:
        return None
    tag_end = html.find(">", match.start())
    if tag_end < 0:
        return None
    tag_end += 1
    depth = 1
    for token in re.finditer(r"</?div\b", html[tag_end:], re.IGNORECASE):
        if token.group(0).startswith("</"):
            depth -= 1
            if depth == 0:
                return html[tag_end : tag_end + token.start()]
        else:
            depth += 1
    return html[tag_end:]


# --- разбор API --------------------------------------------------------------


def parse_api_postings(text: str) -> list[dict]:
    """Разбор ответа ``.../postings/{site}?mode=json``: JSON-массив.

    Returns:
        list[dict]: вакансии с непустым ``id`` (в порядке выдачи).

    Raises:
        ValueError: ответ — не JSON-массив.
        json.JSONDecodeError: не-JSON тело ответа.
    """
    data = json.loads(text)
    if not isinstance(data, list):
        raise ValueError("Lever API: ожидается JSON-массив вакансий")
    return [
        posting
        for posting in data
        if isinstance(posting, dict) and posting.get("id") not in (None, "")
    ]


def _parse_api_posting(text: str) -> dict | None:
    """Разбор ответа одной вакансии ``.../postings/{site}/{id}``."""
    data = json.loads(text)
    if isinstance(data, dict) and data.get("id") not in (None, ""):
        return data
    return None


# --- разбор HTML (fallback) ---------------------------------------------------


def has_next_page(html: str) -> bool:
    """Признак следующей страницы выдачи (у Lever-досок пагинации нет)."""
    return _PAGE_LINK_RE.search(html or "") is not None


def parse_html_postings(html: str) -> list[dict]:
    """Разобрать страницу выдачи ``jobs.lever.co/{site}``.

    Разметка: ``div.posting[data-qa-posting-id]`` → ``a.posting-title[href]``
    с ``h5`` (заголовок) и ``posting-categories`` (span'ы ``location``,
    ``commitment``, ``workplaceTypes``); заголовки групп
    ``div.large-category-header`` дают ``team``.

    Returns:
        list[dict]: карточки ``{id, external_id, title, location, commitment,
        workplace_type, team?, url, company_name?}`` в порядке выдачи,
        без дублей id.
    """
    company_name = None
    title_match = _TITLE_RE.search(html)
    if title_match:
        company_name = _clean_label(title_match.group("text"))

    headers = [
        (match.start(), _clean_label(match.group("text")))
        for match in _GROUP_HEADER_RE.finditer(html)
    ]

    jobs: list[dict] = []
    seen: set[str] = set()
    for match in _TITLE_ANCHOR_RE.finditer(html):
        external_id = _uuid_from(match.group("href"))
        if not external_id or external_id in seen:
            continue

        inner = match.group("inner")
        title = None
        h5 = _H5_RE.search(inner)
        if h5:
            title = _strip_tags(h5.group("text"))
        if not title:
            title = _strip_tags(inner)
        if not title:
            continue

        job: dict[str, Any] = {
            "id": external_id,
            "external_id": external_id,
            "title": title,
            "location": _category_value(inner, "location"),
            "commitment": _category_value(inner, "commitment"),
            "workplace_type": _category_value(inner, "workplaceTypes"),
            "url": _as_text(match.group("href")),
        }
        for position, text in reversed(headers):  # ближайшая шапка сверху
            if position < match.start() and text:
                job["team"] = text
                break
        if company_name:
            job["company_name"] = company_name

        seen.add(external_id)
        jobs.append(job)
    return jobs


def extract_card_fields(html: str) -> dict:
    """Поля вакансии из HTML карточки ``jobs.lever.co/{site}/{id}``.

    Приоритет источников: заголовок (``h2`` в ``.posting-headline``) →
    ``og:title`` → ``<title>Company - Title</title>``; локация/формат —
    категории ``.posting-categories`` (``location``/``commitment``/
    ``workplaceTypes``); описание — секция ``data-qa="job-description"``
    вместе с соседними секциями карточки (внутри общего
    ``section-wrapper``), иначе ``og:description``.

    Returns:
        dict: контент-поля vacancies (docs/02 §3.3): ``title``,
        ``company_name``, ``area``, ``employment_form``, ``work_format``,
        ``description_raw/html`` — только непустые.
    """
    region = _body_region(html)
    fields: dict[str, Any] = {}

    def fill(key: str, value: Any) -> None:
        if value and not fields.get(key):
            fields[key] = value

    # 1. Заголовок: posting-headline (body) → og:title → <title> («Company - Title»).
    #    Мета-теги ищутся по всему документу — они в <head>, не в <body>.
    headline_at = region.find("posting-headline")
    if headline_at >= 0:
        h2 = _H2_RE.search(region, headline_at)
        if h2:
            fill("title", _strip_tags(h2.group("text")))
    og_title = _OG_TITLE_RE.search(html)
    title_element = _TITLE_RE.search(html)
    document_title = _strip_tags(title_element.group("text")) if title_element else None
    for candidate in (
        _strip_tags(og_title.group("text")) if og_title else None,
        document_title,
    ):
        if not candidate:
            continue
        if " - " in candidate:
            company_part, _, title_part = candidate.partition(" - ")
            fill("title", _as_text(title_part))
            fill("company_name", _as_text(company_part))
        else:
            fill("title", candidate)
    if not fields.get("title"):
        h1 = _H1_RE.search(region)
        if h1:
            fill("title", _strip_tags(h1.group("text")))

    # 2. Категории карточки: location / commitment / workplaceTypes.
    fill("area", _category_value(region, "location"))
    fill("employment_form", _category_value(region, "commitment"))
    fill("work_format", _category_value(region, "workplaceTypes"))

    # 3. Описание: секция data-qa="job-description" внутри section-wrapper
    #    (включает соседние секции «Responsibilities» и т.п.), иначе og.
    description_match = _JOB_DESC_OPEN_RE.search(region)
    if description_match:
        wrapper_at = region.rfind("section-wrapper", 0, description_match.start())
        if wrapper_at >= 0:
            opening_start = region.rfind("<div", 0, wrapper_at)
        else:
            opening_start = -1
        if opening_start < 0:
            opening_start = description_match.start()
        description_html = _extract_balanced_at(
            region, opening_start, re.compile(r"<div\b[^>]*", re.IGNORECASE)
        )
        if description_html:
            fill("description_html", description_html.strip() or None)
            fill("description_raw", _strip_tags(description_html))
    og_description = _OG_DESCRIPTION_RE.search(html)
    if og_description:
        fill("description_raw", _as_text(html_lib.unescape(og_description.group("text"))))

    return {key: value for key, value in fields.items() if value}


# --- адаптер -----------------------------------------------------------------


class LeverAdapter(BaseSourceAdapter):
    """Источник Lever: Postings API + HTML-fallback (docs/04 §10)."""

    #: Имя источника в SourceRegistry и в vacancies.source.
    source_name = "lever"

    def __init__(
        self,
        session: AntiBanSession | None = None,
        site: str | None = None,
        region: str | None = None,
    ) -> None:
        # Сессия та же, что у оркестратора: единые прогрев/паузы/прокси и
        # общая статистика трафика (docs/04 §3).
        self.http = session or AntiBanSession(fetcher=build_page_fetcher())
        # Доска по умолчанию: явный слаг либо первый элемент пресета.
        self.site = normalize_site(site) or PRESET_SITES[0]
        # Регион инстанса Lever: None/«us» → глобальный, «eu» → EU.
        self.region = (region or "").lower() or None

    # --- контракт BaseSourceAdapter (docs/04 §10.1) --------------------------

    async def search(self, filters: dict) -> list[dict]:
        """Собрать карточки выдачи доски: API → HTML-fallback (docs/04 §4.1–§4.2).

        Args:
            filters: ``site``/``company`` (слаг доски), ``search_url``
                (готовая ссылка выдачи, §4.2), ``keywords`` (клиентский
                фильтр — Postings API не поддерживает текстовый поиск),
                ``limit``, ``max_pages`` (по умолчанию 1: API и HTML-выдача
                отдают все вакансии разом).

        Returns:
            list[dict]: сырые карточки ``{source, external_id, url, ...}``
            в порядке выдачи, без дублей.
        """
        search_url = filters.get("search_url")
        site = normalize_site(filters.get("site") or filters.get("company"))
        site = site or self.site
        region = self.region
        max_pages = max(1, int(filters.get("max_pages") or 1))
        keywords = filters.get("keywords")
        limit = filters.get("limit")

        if search_url:
            base_url = self.validate_search_url(str(search_url))
            site = _site_from_url(base_url) or site
            region = _region_from_url(base_url) or region
        else:
            base_url = api_url_for(site, region)

        results: list[dict] = []
        seen: set[str] = set()
        for page_index in range(max_pages):
            try:
                postings, has_next = await self._fetch_page(
                    base_url, site, region, page_index
                )
            except Exception as exc:  # noqa: BLE001 — страница не роняет сбор
                logger.warning(
                    "Lever: не удалось получить страницу %s выдачи %s: %s",
                    page_index + 1,
                    base_url,
                    exc,
                )
                break

            for posting in postings:
                external_id = str(
                    posting.get("id") or posting.get("external_id") or ""
                ).strip()
                if not external_id or external_id in seen:
                    continue
                if not _matches_keywords(posting, keywords):
                    continue
                seen.add(external_id)
                results.append(
                    {
                        "source": self.source_name,
                        "external_id": external_id,
                        "url": _as_text(posting.get("hostedUrl") or posting.get("url"))
                        or self.build_vacancy_url(base_url, external_id),
                        **posting,
                    }
                )

            if not has_next:
                break  # API и HTML-выдача отдают все вакансии разом

        if limit is not None:
            results = results[: max(0, int(limit))]
        return results

    async def get_vacancy(self, external_id_or_url: str) -> dict | None:
        """Одна вакансия по id или ссылке (docs/04 §5).

        Основной путь — ``.../postings/{site}/{id}``; при отказе API —
        HTML-карточка ``jobs.lever.co/{site}/{id}``.

        Returns:
            dict | None: сырые данные вакансии либо None, если вакансия
            не найдена/удалена на источнике.
        """
        resolved = self._resolve_job(external_id_or_url)
        if not resolved:
            return None
        site, external_id, region = resolved

        # 1) Основной путь: API одной вакансии.
        try:
            response = await self.http.fetch(
                api_posting_url_for(site, external_id, region)
            )
            if response.status_code == 200:
                posting = _parse_api_posting(response.text)
                if posting:
                    return self._raw_posting(posting, site, external_id)
            raise LeverAPIError(
                f"Lever API вернул {response.status_code} для {external_id}"
            )
        except Exception as exc:  # noqa: BLE001 — любой сбой → HTML-fallback
            logger.warning(
                "Lever API недоступен для вакансии %s, пробую HTML: %s",
                external_id,
                exc,
            )

        # 2) Fallback: карточка в HTML.
        card_url = job_url_for(site, external_id, region)
        try:
            response = await self.http.fetch(card_url)
        except Exception as exc:  # noqa: BLE001
            logger.debug("Lever: HTML-карточка недоступна: %s", exc)
            return None
        if response.status_code != 200:
            return None  # 404/410 — вакансия удалена (docs/04 §5)
        fields = extract_card_fields(response.text)
        if not fields.get("title"):
            return None
        return {
            "source": self.source_name,
            "external_id": external_id,
            "url": card_url,
            **fields,
        }

    def normalize(self, raw: dict) -> dict:
        """Привести сырые данные вакансии к канонической схеме (docs/04 §7)."""
        external_id = _as_text(raw.get("id") or raw.get("external_id"))
        if not external_id:
            raise ValueError("Vacancy missing external_id")

        url = _as_text(raw.get("hostedUrl") or raw.get("url"))
        site = _site_from_url(url) or self.site
        if not url:
            url = job_url_for(site, external_id, self.region)

        categories = raw.get("categories")
        categories = categories if isinstance(categories, dict) else {}

        # Локация: API → categories.location (строка или allLocations-список),
        # HTML-fallback → строка в key «location».
        location = (
            raw.get("area") or raw.get("location") or categories.get("location")
        )
        if isinstance(location, (list, tuple)):
            location = ", ".join(str(item) for item in location if item)
        area = _as_text(location, limit=255) or ""

        description_html = _as_text(raw.get("description_html") or raw.get("description"))
        description_raw = _as_text(
            raw.get("description_raw") or raw.get("descriptionPlain")
        )
        if not description_raw and description_html:
            description_raw = _strip_tags(description_html)

        work_format = _normalize_work_format(
            raw.get("work_format")
            or raw.get("workplace_type")
            or raw.get("workplaceType")
        )
        if not work_format and "remote" in area.casefold():
            work_format = "remote"

        employment_form = _as_text(
            raw.get("employment_form")
            or raw.get("commitment")
            or categories.get("commitment"),
            limit=64,
        )

        # Дата публикации: готовое поле либо createdAt (epoch мс) → ISO-8601.
        published_at = _as_text(raw.get("published_at"))
        if not published_at:
            published_at = _published_at_from_millis(
                raw.get("createdAt") or raw.get("created_at")
            )

        salary_range = raw.get("salaryRange")
        salary_range = salary_range if isinstance(salary_range, dict) else {}
        salary_from = raw.get("salary_from")
        if salary_from is None:
            salary_from = salary_range.get("min")
        salary_to = raw.get("salary_to")
        if salary_to is None:
            salary_to = salary_range.get("max")
        salary_currency = _as_text(
            raw.get("salary_currency") or salary_range.get("currency"), limit=8
        )

        return {
            "source": self.source_name,
            "external_id": external_id,
            "url": url,
            "title": _as_text(raw.get("text") or raw.get("title"), limit=512),
            "company_name": _as_text(raw.get("company_name"), limit=512)
            or company_name_for(site),
            "salary_from": salary_from,
            "salary_to": salary_to,
            "salary_currency": salary_currency,
            "experience": _as_text(raw.get("experience"), limit=64),
            "employment_form": employment_form,
            "work_format": _as_text(work_format, limit=64),
            "schedule": _as_text(raw.get("schedule"), limit=128),
            "area": area,
            "published_at": published_at,
            "description_raw": description_raw,
            "description_html": description_html,
        }

    def build_search_url(
        self,
        *,
        site: str | None = None,
        company: str | None = None,
        region: str | None = None,
        page: int = 0,
        **_kwargs: object,
    ) -> str:
        """Ссылка выдачи автопоиска — Postings API доски (docs/04 §4.1).

        Lever API не принимает текстовый запрос: ``keywords`` фильтруются
        на клиенте в :meth:`search`; остальные фильтры автопоиска источнику
        недоступны и игнорируются.
        """
        del page  # API отдаёт все вакансии разом
        slug = normalize_site(site or company) or self.site
        return api_url_for(slug, region or self.region)

    def validate_search_url(self, search_url: str) -> str:
        """Принимаются только ссылки API/выдачи Lever (docs/04 §4.2)."""
        candidate = str(search_url).strip()
        parts = urlsplit(candidate)
        host = (parts.hostname or "").lower()
        if parts.scheme not in ("http", "https") or host not in _ALLOWED_HOSTS:
            raise ValueError(
                "Ссылка выдачи Lever должна быть на jobs.lever.co или "
                f"api.lever.co: {candidate!r}"
            )
        return candidate

    def page_url(self, base_url: str, page: int) -> str:
        """Страница выдачи: Postings API и HTML-выдача Lever не пагинируют
        (все вакансии на одной странице) — ссылка возвращается без изменений."""
        del page
        return base_url

    def parse_listing(self, html: str) -> tuple[list[str], bool]:
        """Разобрать выдачу: (external_id в порядке выдачи, есть следующая).

        Текст может быть JSON-ответом API или HTML-фолбэком — формат
        определяется автоматически.
        """
        try:
            postings = parse_api_postings(html)
        except (json.JSONDecodeError, ValueError):
            postings = None
        if postings is not None:
            return [str(posting["id"]) for posting in postings], False
        html_postings = parse_html_postings(html)
        return (
            [posting["external_id"] for posting in html_postings],
            has_next_page(html),
        )

    def build_vacancy_url(self, base_url: str, external_id: str) -> str:
        """Прямая ссылка карточки: ``jobs.lever.co/{site}/{id}``."""
        external_id = str(external_id).strip()
        if not external_id:
            return base_url
        site = _site_from_url(base_url) or self.site
        region = _region_from_url(base_url) or self.region
        return job_url_for(site, external_id, region)

    def extract_fields(self, html: str) -> dict:
        """Извлечь поля вакансии из HTML карточки (docs/04 §7)."""
        return extract_card_fields(html)

    async def _fetch_page(
        self, base_url: str, site: str, region: str | None, page_index: int
    ) -> tuple[list[dict], bool]:
        """Одна страница выдачи: основной путь API → fallback HTML."""
        if _is_api_url(base_url):
            try:
                response = await self.http.fetch(base_url)
                if response.status_code != 200:
                    raise LeverAPIError(
                        f"Lever API вернул {response.status_code}: {base_url}"
                    )
                return parse_api_postings(response.text), False
            except Exception as exc:  # noqa: BLE001 — любая ошибка → HTML-fallback
                logger.warning(
                    "Lever API недоступен (%s), переключаюсь на HTML-выдачу %s",
                    exc,
                    listing_url_for(site, region),
                )
            url = self.page_url(listing_url_for(site, region), page_index)
        else:
            url = self.page_url(base_url, page_index)

        response = await self.http.fetch(url)
        if response.status_code == 200:
            return parse_html_postings(response.text), has_next_page(response.text)
        if response.status_code in _NOT_FOUND_STATUSES:
            return [], False
        raise LeverAPIError(f"Lever HTML-выдача вернула {response.status_code}: {url}")

    def _resolve_job(
        self, external_id_or_url: str
    ) -> tuple[str, str, str | None] | None:
        """``(site, external_id, region)`` из id или прямой ссылки (иначе None)."""
        value = (external_id_or_url or "").strip()
        if not value:
            return None
        if value.startswith(("http://", "https://")):
            site = _site_from_url(value)
            external_id = _uuid_from(value)
            region = _region_from_url(value)
        else:
            site, external_id, region = self.site, _uuid_from(value), self.region
        if not site or not external_id:
            return None
        return site, external_id, region

    def _raw_posting(self, posting: dict, site: str, external_id: str) -> dict:
        """Сырая карточка вакансии в формате search/get_vacancy."""
        resolved_id = str(posting.get("id") or external_id)
        return {
            "source": LeverAdapter.source_name,
            "external_id": resolved_id,
            "url": _as_text(posting.get("hostedUrl") or posting.get("url"))
            or job_url_for(site, resolved_id, self.region),
            **posting,
        }


def _normalize_work_format(value: object) -> str | None:
    """``workplaceType`` Lever → канонический ``work_format``."""
    text = _as_text(value, limit=64)
    if not text:
        return None
    lowered = text.casefold().replace(" ", "")
    if lowered == "remote":
        return "remote"
    if lowered == "hybrid":
        return "hybrid"
    if lowered in ("on-site", "onsite", "office"):
        return "onsite"
    return text


def _published_at_from_millis(value: object) -> str | None:
    """``createdAt`` Lever (epoch мс) → ISO-8601 UTC."""
    if value in (None, ""):
        return None
    try:
        seconds = int(value) / 1000
    except (TypeError, ValueError):
        return None
    try:
        return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def _matches_keywords(posting: dict, keywords: object) -> bool:
    """Клиентский фильтр: любое слово есть в заголовке/описании/локации."""
    if not keywords:
        return True
    if isinstance(keywords, str):
        keywords = [keywords]
    terms = [str(term).strip().casefold() for term in keywords if str(term).strip()]
    if not terms:
        return True
    categories = posting.get("categories")
    categories = categories if isinstance(categories, dict) else {}
    haystack_parts = [
        str(posting.get("text") or posting.get("title") or ""),
        str(posting.get("location") or categories.get("location") or ""),
        str(posting.get("team") or categories.get("team") or ""),
        str(posting.get("descriptionPlain") or posting.get("description_raw") or ""),
        str(posting.get("description") or posting.get("description_html") or ""),
    ]
    haystack = _strip_tags(" ".join(haystack_parts)) or ""
    haystack = haystack.casefold()
    return any(term in haystack for term in terms)
