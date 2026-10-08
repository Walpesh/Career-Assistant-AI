"""Greenhouse Job Board source adapter (docs/04_PARSING_RULES.md §10).

Площадка — публичные доски вакансий Greenhouse (``board_token`` компании).
Два пути получения данных:

- **Основной** — публичный Job Board API
  ``https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs?content=true``
  (без ключа): JSON ``{"jobs": [...]}`` с полями ``id``, ``title``,
  ``absolute_url``, ``location.name``, ``updated_at``/``first_published``,
  ``company_name`` и ``content`` (HTML-описание). Запрос идёт через общую
  ``AntiBanSession`` оркестратора.
- **Fallback** — при отказе/непригодности API (5xx, 404, не-JSON, сетевая
  ошибка) HTML-разбор страницы выдачи ``https://boards.greenhouse.io/{token}``
  (разметка ``tr.job-post`` со ссылками ``.../jobs/{id}``, пагинация
  ``?page=N``, см. ``parse_html_jobs``/``has_next_page``).

Доска задаётся явно: ``board_token`` в конструкторе либо в фильтрах
``search``/``build_search_url`` (``board_token``, ``search_url``). Без явного
токена используется пресет целевых tech-компаний ``PRESET_BOARD_TOKENS``
(первый элемент — доска по умолчанию).

Нормализация (``normalize``) приводит сырые данные к канонической схеме
docs/04 §7 (ключи ``source``, ``external_id``, ``url``, ``title``,
``company_name``, ``salary_from/to/currency``, ``experience``,
``employment_form``, ``work_format``, ``schedule``, ``area``,
``published_at``, ``description_raw``, ``description_html``); зарплата на
досках Greenhouse не публикуется — ``salary_*`` всегда None, если её нет в
сырых данных. ``work_format`` выводится из локации («Remote, …» → remote).

Anti-ban (прогрев, паузы 4–8 с, ротация прокси) остаётся в Proxy & Anti-Ban
Module: API и карточки запрашиваются через готовую ``AntiBanSession``.
"""

from __future__ import annotations

import html as html_lib
import json
import logging
import re
from typing import Any
from urllib.parse import urlsplit

from app.modules.anti_ban import AntiBanSession
from app.modules.parsing.fetcher import build_page_fetcher
from app.modules.parsing.sources.base import BaseSourceAdapter

__all__ = [
    "API_BASE",
    "LISTING_BASE",
    "JOB_BASE",
    "PRESET_BOARD_TOKENS",
    "PRESET_COMPANY_NAMES",
    "GreenhouseAdapter",
    "GreenhouseAPIError",
    "api_url_for",
    "company_name_for",
    "extract_card_fields",
    "has_next_page",
    "job_url_for",
    "listing_url_for",
    "normalize_board_token",
    "parse_api_jobs",
    "parse_html_jobs",
]

logger = logging.getLogger(__name__)

#: Публичный Job Board API Greenhouse (без ключа).
API_BASE = "https://boards-api.greenhouse.io/v1/boards"
#: Страницы выдачи доски — цель HTML-fallback при отказе API.
LISTING_BASE = "https://boards.greenhouse.io"
#: Карточки вакансий (``absolute_url`` API и ссылки выдачи).
JOB_BASE = "https://job-boards.greenhouse.io"

#: Пресет целевых tech-компаний: используется, когда ``board_token`` не задан.
PRESET_BOARD_TOKENS: tuple[str, ...] = (
    "gitlab",
    "airbnb",
    "spotify",
    "figma",
    "datadog",
    "cloudflare",
)

#: Отображаемое имя компании для токенов пресета (фолбэк company_name).
PRESET_COMPANY_NAMES: dict[str, str] = {
    "gitlab": "GitLab",
    "airbnb": "Airbnb",
    "spotify": "Spotify",
    "figma": "Figma",
    "datadog": "Datadog",
    "cloudflare": "Cloudflare",
}

#: Разрешённые хосты ссылок выдачи/карточек (docs/04 §4.2 — белый список).
_ALLOWED_HOSTS = frozenset(
    {
        "boards.greenhouse.io",
        "www.boards.greenhouse.io",
        "job-boards.greenhouse.io",
        "www.job-boards.greenhouse.io",
        "boards-api.greenhouse.io",
    }
)
#: Статусы «вакансия удалена» (docs/04 §5 → get_vacancy вернёт None).
_NOT_FOUND_STATUSES = (404, 410)

#: Ссылка карточки в выдаче: ``.../jobs/<digits>`` (новая и старая вёрстки).
_JOB_ANCHOR_RE = re.compile(
    r'<a\b[^>]*?href=["\'](?P<href>[^"\']*/jobs/(?P<external_id>\d+)[^"\']*)["\']'
    r"[^>]*>(?P<inner>.*?)</a>",
    re.IGNORECASE | re.DOTALL,
)
#: Заголовок в новой вёрстке: <p class="body body--medium">Title</p>.
_TITLE_P_RE = re.compile(
    r'<p\b[^>]*class=["\'][^"\']*\bbody--medium\b[^"\']*["\'][^>]*>(?P<text>.*?)</p>',
    re.IGNORECASE | re.DOTALL,
)
#: Локация в новой вёрстке: <p class="body body__secondary body--metadata">.
_LOCATION_P_RE = re.compile(
    r'<p\b[^>]*class=["\'][^"\']*\bbody--metadata\b[^"\']*["\'][^>]*>(?P<text>.*?)</p>',
    re.IGNORECASE | re.DOTALL,
)
#: Старая вёрстка: локация — соседний <div class="location"> после ссылки.
_LEGACY_LOCATION_RE = re.compile(
    r'<div\b[^>]*class=["\'][^"\']*\blocation\b[^"\']*["\'][^>]*>(?P<text>.*?)</div>',
    re.IGNORECASE | re.DOTALL,
)
#: Заголовок страницы выдачи: «Jobs at GitLab» → компания.
_BOARD_TITLE_RE = re.compile(
    r"<title[^>]*>\s*Jobs\s+at\s+(?P<company>[^<|]+?)\s*(?:\||</title>)",
    re.IGNORECASE,
)
#: Пагинация новой вёрстки: <nav class="pagination ...">.
_PAGINATION_NAV_RE = re.compile(
    r'<nav\b[^>]*class=["\'][^"\']*\bpagination\b', re.IGNORECASE
)
#: Последняя страница: кнопка «Next» неактивна.
_NEXT_INACTIVE_RE = re.compile(r"pagination__next--inactive", re.IGNORECASE)
#: Старая вёрстка: ссылки вида «?page=2».
_PAGE_LINK_RE = re.compile(r'href=["\'][^"\']*[?&]page=\d+', re.IGNORECASE)

#: Карточка вакансии: заголовок и локация.
_H1_RE = re.compile(r"<h1\b[^>]*>(?P<text>.*?)</h1>", re.IGNORECASE | re.DOTALL)
_JOB_LOCATION_RE = re.compile(
    r'<div\b[^>]*class=["\'][^"\']*\bjob__location\b[^"\']*["\'][^>]*>'
    r"(?P<text>.*?)</div>",
    re.IGNORECASE | re.DOTALL,
)
#: Карточка: <div class="job__description body"> с вложенными div —
#: извлекается сбалансированно (см. ``_extract_balanced_div``).
_JOB_DESCRIPTION_OPEN_RE = re.compile(
    r'<div\b[^>]*class=["\'][^"\']*\bjob__description\b', re.IGNORECASE
)
#: <title>Job Application for {title} at {company}</title>.
_CARD_TITLE_RE = re.compile(
    r"<title[^>]*>\s*Job\s+Application\s+for\s+(?P<title>.+?)\s+at\s+"
    r"(?P<company>.+?)\s*</title>",
    re.IGNORECASE,
)
#: Мета-теги карточки.
_OG_TITLE_RE = re.compile(
    r'<meta[^>]*property=["\']og:title["\'][^>]*content=["\'](?P<text>[^"\']*)["\']',
    re.IGNORECASE,
)
_OG_DESCRIPTION_RE = re.compile(
    r'<meta[^>]*property=["\']og:description["\'][^>]*content=["\'](?P<text>[^"\']*)["\']',
    re.IGNORECASE,
)
#: Данные вакансии в контексте Remix: window.__remixContext = {...}.
_REMIX_MARKER = "window.__remixContext"
_REMIX_JOB_POST_KEY = "jobPost"


class GreenhouseAPIError(RuntimeError):
    """Ошибка Job Board API Greenhouse (не-JSON, 4xx/5xx, сетевой сбой)."""



# --- URL и вспомогательные функции -------------------------------------------


def normalize_board_token(value: object) -> str | None:
    """Нормализовать ``board_token``: обрезка, слэши, валидация символов.

    Returns:
        str | None: чистый токен либо None для пустого значения.

    Raises:
        ValueError: токен содержит недопустимые символы.
    """
    if value is None:
        return None
    token = str(value).strip().strip("/")
    if not token:
        return None
    if not re.fullmatch(r"[A-Za-z0-9_\-]+", token):
        raise ValueError(f"Некорректный Greenhouse board_token: {value!r}")
    return token


def api_url_for(board_token: str) -> str:
    """Ссылка Job Board API доски (``content=true`` — описания в JSON)."""
    return f"{API_BASE}/{board_token}/jobs?content=true"


def listing_url_for(board_token: str) -> str:
    """Страница выдачи доски — цель HTML-fallback."""
    return f"{LISTING_BASE}/{board_token}"


def job_url_for(board_token: str, external_id: str) -> str:
    """Прямая ссылка карточки вакансии доски."""
    return f"{JOB_BASE}/{board_token}/jobs/{external_id}"


def company_name_for(board_token: str | None) -> str | None:
    """Отображаемое имя компании для токена пресета (иначе None)."""
    if not board_token:
        return None
    return PRESET_COMPANY_NAMES.get(board_token)


def _is_api_url(url: str) -> bool:
    """Правда, если ссылка указывает на Job Board API Greenhouse."""
    return "boards-api.greenhouse.io" in (url or "")


def _token_from_url(url: object) -> str | None:
    """``board_token`` из ссылки API/выдачи/карточки (иначе None)."""
    value = _as_text(url)
    if not value or "greenhouse.io" not in value:
        return None
    parts = [part for part in urlsplit(value).path.split("/") if part]
    if not parts:
        return None
    if "boards" in parts:  # /v1/boards/<token>/jobs/...
        index = parts.index("boards")
        candidate = parts[index + 1] if index + 1 < len(parts) else None
    elif "jobs" in parts:  # /<token>/jobs/<id>
        index = parts.index("jobs")
        candidate = parts[index - 1] if index else None
    else:  # boards.greenhouse.io/<token>
        candidate = parts[0]
    try:
        return normalize_board_token(candidate)
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


# --- разбор API --------------------------------------------------------------


def parse_api_jobs(text: str) -> list[dict]:
    """Разбор ответа ``.../jobs?content=true``: ``{"jobs": [...]}``.

    Returns:
        list[dict]: вакансии с непустым ``id`` (в порядке выдачи).

    Raises:
        ValueError: не-JSON-объект или отсутствует список ``jobs``.
        json.JSONDecodeError: не-JSON тело ответа.
    """
    data = json.loads(text)
    if not isinstance(data, dict) or not isinstance(data.get("jobs"), list):
        raise ValueError("Greenhouse API: в ответе нет списка «jobs»")
    return [
        job
        for job in data["jobs"]
        if isinstance(job, dict) and job.get("id") not in (None, "")
    ]


def _parse_api_job(text: str) -> dict | None:
    """Разбор ответа одного ``.../jobs/{id}?content=true`` → объект вакансии."""
    data = json.loads(text)
    if isinstance(data, dict) and isinstance(data.get("jobs"), list):
        jobs = [job for job in data["jobs"] if isinstance(job, dict)]
        return jobs[0] if jobs else None
    if isinstance(data, dict) and data.get("id") not in (None, ""):
        return data
    return None


# --- разбор HTML (fallback) ---------------------------------------------------


def has_next_page(html: str) -> bool:
    """Признак наличия следующей страницы выдачи."""
    if _PAGINATION_NAV_RE.search(html):
        return _NEXT_INACTIVE_RE.search(html) is None
    return _PAGE_LINK_RE.search(html) is not None


def parse_html_jobs(html: str) -> list[dict]:
    """Разобрать страницу выдачи ``boards.greenhouse.io/{token}``.

    Понимает новую вёрстку (``tr.job-post`` → ``<a ... jobs/{id}><p class=
    "body body--medium">Название</p><p class="...body--metadata">Локация``)
    и старую (``<div class="opening">`` с локацией-соседом ссылки).

    Returns:
        list[dict]: карточки ``{id, external_id, title, location, url,
        company_name?}`` в порядке выдачи, без дублей id.
    """
    company_name = None
    title_match = _BOARD_TITLE_RE.search(html)
    if title_match:
        company_name = _as_text(title_match.group("company"), limit=512)

    jobs: list[dict] = []
    seen: set[str] = set()
    for match in _JOB_ANCHOR_RE.finditer(html):
        external_id = match.group("external_id")
        if external_id in seen:
            continue

        inner = match.group("inner")
        title = None
        title_p = _TITLE_P_RE.search(inner)
        if title_p:
            title = _strip_tags(title_p.group("text"))
        if not title:
            title = _strip_tags(inner)
        if not title:
            continue

        location = None
        location_p = _LOCATION_P_RE.search(inner)
        if location_p:
            location = _strip_tags(location_p.group("text"))
        if not location:
            # Старая вёрстка: <div class="location"> идёт после ссылки.
            context = html[match.end() : match.end() + 400]
            legacy = _LEGACY_LOCATION_RE.search(context)
            if legacy:
                location = _strip_tags(legacy.group("text"))

        seen.add(external_id)
        job: dict[str, Any] = {
            "id": external_id,
            "external_id": external_id,
            "title": title,
            "location": location,
            "url": _as_text(match.group("href")),
        }
        if company_name:
            job["company_name"] = company_name
        jobs.append(job)
    return jobs


def _extract_balanced_div(html: str, opening_re: re.Pattern[str]) -> str | None:
    """Извлечь содержимое первого ``<div class=...>`` с учётом вложенности."""
    match = opening_re.search(html)
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


def _remix_job_post(html: str) -> dict | None:
    """Данные вакансии из ``window.__remixContext`` (JSON, точный парс)."""
    marker_index = html.find(_REMIX_MARKER)
    if marker_index < 0:
        return None
    brace_index = html.find("{", marker_index)
    if brace_index < 0:
        return None
    try:
        data, _ = json.JSONDecoder().raw_decode(html[brace_index:])
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    state = data.get("state")
    loader = state.get("loaderData") if isinstance(state, dict) else None
    if not isinstance(loader, dict):
        return None
    for value in loader.values():
        if isinstance(value, dict) and isinstance(value.get(_REMIX_JOB_POST_KEY), dict):
            return value[_REMIX_JOB_POST_KEY]
    return None


def extract_card_fields(html: str) -> dict:
    """Поля вакансии из HTML карточки ``job-boards.greenhouse.io/.../jobs/{id}``.

    Приоритет источников: заголовок (``h1`` в ``job__title``) → ``og:title`` →
    ``<title>Job Application for {title} at {company}</title>``; описание —
    сбалансированный блок ``job__description`` → ``content`` из
    ``window.__remixContext``; локация — ``job__location`` → ``og:description``.
    Возвращаются только контент-поля vacancies (docs/02 §3.3): ``title``,
    ``company_name``, ``area``, ``description_raw/html``.
    """
    fields: dict[str, Any] = {}

    def fill(key: str, value: Any) -> None:
        if value and not fields.get(key):
            fields[key] = value

    # 1. Заголовок и компания.
    h1 = _H1_RE.search(html)
    if h1:
        fill("title", _strip_tags(h1.group("text")))
    og_title = _OG_TITLE_RE.search(html)
    if og_title:
        fill("title", _as_text(html_lib.unescape(og_title.group("text"))))
    card_title = _CARD_TITLE_RE.search(html)
    if card_title:
        fill("title", _as_text(card_title.group("title")))
        fill("company_name", _as_text(card_title.group("company")))

    # 2. Локация (job__location содержит svg — текст очищается от тегов).
    location = _JOB_LOCATION_RE.search(html)
    if location:
        fill("area", _strip_tags(location.group("text")))
    og_description = _OG_DESCRIPTION_RE.search(html)
    if og_description:
        fill("area", _as_text(html_lib.unescape(og_description.group("text"))))

    # 3. Описание: балансированный блок → Remix-контекст.
    description_html = _extract_balanced_div(html, _JOB_DESCRIPTION_OPEN_RE)
    if description_html:
        fill("description_html", description_html.strip() or None)
        fill("description_raw", _strip_tags(description_html))
    else:
        job_post = _remix_job_post(html)
        if job_post:
            fill("title", _as_text(job_post.get("title")))
            content = _as_text(job_post.get("content"))
            if content:
                fill("description_html", content)
                fill("description_raw", _strip_tags(content))

    return {key: value for key, value in fields.items() if value}


# --- адаптер -----------------------------------------------------------------


class GreenhouseAdapter(BaseSourceAdapter):
    """Источник Greenhouse: Job Board API + HTML-fallback (docs/04 §10)."""

    #: Имя источника в SourceRegistry и в vacancies.source.
    source_name = "greenhouse"

    def __init__(
        self,
        session: AntiBanSession | None = None,
        board_token: str | None = None,
    ) -> None:
        # Сессия та же, что у оркестратора: единые прогрев/паузы/прокси и
        # общая статистика трафика (docs/04 §3).
        self.http = session or AntiBanSession(fetcher=build_page_fetcher())
        # Доска по умолчанию: явный токен либо первый элемент пресета.
        self.board_token = normalize_board_token(board_token) or PRESET_BOARD_TOKENS[0]

    # --- контракт BaseSourceAdapter (docs/04 §10.1) --------------------------

    async def search(self, filters: dict) -> list[dict]:
        """Собрать карточки выдачи доски: API → HTML-fallback (docs/04 §4.1–§4.2).

        Args:
            filters: ``board_token`` (доска), ``search_url`` (готовая ссылка
                выдачи, §4.2), ``keywords`` (клиентский фильтр — Job Board API
                не поддерживает текстовый поиск), ``limit``, ``max_pages``
                (по умолчанию 1: API отдаёт все вакансии разом, HTML-фолбэк
                обходит ``?page=N``).

        Returns:
            list[dict]: сырые карточки ``{source, external_id, url, ...}``
            в порядке выдачи, без дублей.
        """
        search_url = filters.get("search_url")
        board_token = normalize_board_token(filters.get("board_token"))
        board_token = board_token or self.board_token
        max_pages = max(1, int(filters.get("max_pages") or 1))
        keywords = filters.get("keywords")
        limit = filters.get("limit")

        if search_url:
            base_url = self.validate_search_url(str(search_url))
            board_token = _token_from_url(base_url) or board_token
        else:
            base_url = api_url_for(board_token)

        results: list[dict] = []
        seen: set[str] = set()
        for page_index in range(max_pages):
            try:
                jobs, has_next = await self._fetch_page(base_url, board_token, page_index)
            except Exception as exc:  # noqa: BLE001 — страница не роняет сбор
                logger.warning(
                    "Greenhouse: не удалось получить страницу %s выдачи %s: %s",
                    page_index + 1,
                    base_url,
                    exc,
                )
                break

            for job in jobs:
                external_id = str(job.get("id") or job.get("external_id") or "").strip()
                if not external_id or external_id in seen:
                    continue
                if not _matches_keywords(job, keywords):
                    continue
                seen.add(external_id)
                results.append(
                    {
                        "source": self.source_name,
                        "external_id": external_id,
                        "url": _as_text(job.get("url") or job.get("absolute_url"))
                        or self.build_vacancy_url(base_url, external_id),
                        **job,
                    }
                )

            if not has_next:
                break  # API отдаёт всё разом; у HTML кончилась пагинация

        if limit is not None:
            results = results[: max(0, int(limit))]
        return results

    async def get_vacancy(self, external_id_or_url: str) -> dict | None:
        """Одна вакансия по id или ссылке (docs/04 §5).

        Основной путь — ``.../boards/{token}/jobs/{id}?content=true``;
        при отказе API — HTML-карточка ``job-boards.greenhouse.io``.

        Returns:
            dict | None: сырые данные вакансии либо None, если вакансия
            не найдена/удалена на источнике.
        """
        resolved = self._resolve_job(external_id_or_url)
        if not resolved:
            return None
        board_token, external_id = resolved

        # 1) Основной путь: API одной вакансии.
        try:
            response = await self.http.fetch(
                f"{API_BASE}/{board_token}/jobs/{external_id}?content=true"
            )
            if response.status_code == 200:
                job = _parse_api_job(response.text)
                if job:
                    return self._raw_job(job, board_token, external_id)
            raise GreenhouseAPIError(
                f"Greenhouse API вернул {response.status_code} для {external_id}"
            )
        except Exception as exc:  # noqa: BLE001 — любой сбой → HTML-fallback
            logger.warning(
                "Greenhouse API недоступен для вакансии %s, пробую HTML: %s",
                external_id,
                exc,
            )

        # 2) Fallback: карточка в HTML.
        card_url = job_url_for(board_token, external_id)
        try:
            response = await self.http.fetch(card_url)
        except Exception as exc:  # noqa: BLE001
            logger.debug("Greenhouse: HTML-карточка недоступна: %s", exc)
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

        url = _as_text(raw.get("url") or raw.get("absolute_url"))
        board_token = _token_from_url(url) or self.board_token
        if not url:
            url = job_url_for(board_token, external_id)

        # Локация: API отдаёт {"name": ...}, HTML-fallback — строку.
        location = raw.get("location")
        if isinstance(location, dict):
            location = location.get("name")
        area = _as_text(raw.get("area") or location, limit=255) or ""

        description_html = _as_text(raw.get("description_html") or raw.get("content"))
        description_raw = _as_text(raw.get("description_raw")) or (
            _strip_tags(description_html) if description_html else None
        )

        work_format = _as_text(raw.get("work_format"), limit=64)
        if not work_format and "remote" in area.casefold():
            work_format = "remote"

        return {
            "source": self.source_name,
            "external_id": external_id,
            "url": url,
            "title": _as_text(raw.get("title"), limit=512),
            "company_name": _as_text(raw.get("company_name"), limit=512)
            or company_name_for(board_token),
            "salary_from": raw.get("salary_from"),
            "salary_to": raw.get("salary_to"),
            "salary_currency": _as_text(raw.get("salary_currency"), limit=8),
            "experience": _as_text(raw.get("experience"), limit=64),
            "employment_form": _as_text(raw.get("employment_form"), limit=64),
            "work_format": work_format,
            "schedule": _as_text(raw.get("schedule"), limit=128),
            "area": area,
            "published_at": _as_text(
                raw.get("published_at")
                or raw.get("first_published")
                or raw.get("updated_at")
            ),
            "description_raw": description_raw,
            "description_html": description_html,
        }

    # --- хуки ParsingOrchestrator (docs/04 §10.2) ----------------------------

    def build_search_url(
        self,
        *,
        keywords: list[str] | None = None,
        employment_forms: list[str] | None = None,
        work_formats: list[str] | None = None,
        schedules: list[str] | None = None,
        board_token: str | None = None,
        page: int = 0,
        **_kwargs: object,
    ) -> str:
        """Ссылка выдачи автопоиска — Job Board API доски (docs/04 §4.1).

        Greenhouse API не принимает текстовый запрос: ``keywords`` фильтруются
        на клиенте в :meth:`search`; остальные фильтры автопоиска источнику
        недоступны и игнорируются.
        """
        token = normalize_board_token(board_token) or self.board_token
        return api_url_for(token)

    def validate_search_url(self, search_url: str) -> str:
        """Принимаются только ссылки выдачи Greenhouse (docs/04 §4.2)."""
        candidate = str(search_url).strip()
        parts = urlsplit(candidate)
        host = (parts.hostname or "").lower()
        if parts.scheme not in ("http", "https") or host not in _ALLOWED_HOSTS:
            raise ValueError(
                "Ссылка выдачи Greenhouse должна быть на boards.greenhouse.io: "
                f"{candidate!r}"
            )
        return candidate

    def page_url(self, base_url: str, page: int) -> str:
        """Страница выдачи: API отдаёт все вакансии разом (пагинация
        игнорируется), HTML-выдача обходит ``?page=N`` (1-based, как на доске)."""
        if _is_api_url(base_url):
            return base_url
        index = max(0, int(page or 0))
        if index == 0:
            return base_url
        joiner = "&" if "?" in base_url else "?"
        return f"{base_url}{joiner}page={index + 1}"

    def parse_listing(self, html: str) -> tuple[list[str], bool]:
        """Разобрать страницу выдачи: (external_id в порядке выдачи, есть следующая).

        Текст может быть JSON-ответом API или HTML-фолбэком — формат
        определяется автоматически.
        """
        try:
            jobs = parse_api_jobs(html)
        except (json.JSONDecodeError, ValueError):
            jobs = None
        if jobs is not None:
            return [str(job["id"]) for job in jobs], False
        html_jobs = parse_html_jobs(html)
        return [job["external_id"] for job in html_jobs], has_next_page(html)

    def build_vacancy_url(self, base_url: str, external_id: str) -> str:
        """Прямая ссылка карточки: ``job-boards.greenhouse.io/{token}/jobs/{id}``."""
        external_id = str(external_id).strip()
        if not external_id:
            return base_url
        board_token = _token_from_url(base_url) or self.board_token
        return job_url_for(board_token, external_id)

    def extract_fields(self, html: str) -> dict:
        """Извлечь поля вакансии из HTML карточки (docs/04 §7)."""
        return extract_card_fields(html)

    # --- вспомогательное -----------------------------------------------------

    async def _fetch_page(
        self, base_url: str, board_token: str, page_index: int
    ) -> tuple[list[dict], bool]:
        """Одна страница выдачи: основной путь API → fallback HTML."""
        if _is_api_url(base_url):
            try:
                response = await self.http.fetch(base_url)
                if response.status_code != 200:
                    raise GreenhouseAPIError(
                        f"Greenhouse API вернул {response.status_code}: {base_url}"
                    )
                return parse_api_jobs(response.text), False
            except Exception as exc:  # noqa: BLE001 — любая ошибка → HTML-fallback
                logger.warning(
                    "Greenhouse API недоступен (%s), переключаюсь на HTML-выдачу %s",
                    exc,
                    listing_url_for(board_token),
                )
            url = self.page_url(listing_url_for(board_token), page_index)
        else:
            url = self.page_url(base_url, page_index)

        response = await self.http.fetch(url)
        if response.status_code == 200:
            return parse_html_jobs(response.text), has_next_page(response.text)
        if response.status_code in _NOT_FOUND_STATUSES:
            return [], False
        raise GreenhouseAPIError(
            f"Greenhouse HTML-выдача вернула {response.status_code}: {url}"
        )

    def _resolve_job(self, external_id_or_url: str) -> tuple[str, str] | None:
        """``(board_token, external_id)`` из id или прямой ссылки (иначе None)."""
        value = (external_id_or_url or "").strip()
        if not value:
            return None
        if value.startswith(("http://", "https://")):
            parts = [part for part in urlsplit(value).path.split("/") if part]
            if "jobs" not in parts:
                return None
            index = parts.index("jobs")
            if index == 0 or index + 1 >= len(parts):
                return None
            board_token = normalize_board_token(parts[index - 1])
            external_id = parts[index + 1]
        else:
            board_token, external_id = self.board_token, value
        if not board_token or not re.fullmatch(r"\d{1,32}", external_id):
            return None
        return board_token, external_id

    @staticmethod
    def _raw_job(job: dict, board_token: str, external_id: str) -> dict:
        """Сырая карточка вакансии в формате search/get_vacancy."""
        resolved_id = str(job.get("id") or external_id)
        return {
            "source": GreenhouseAdapter.source_name,
            "external_id": resolved_id,
            "url": _as_text(job.get("absolute_url") or job.get("url"))
            or job_url_for(board_token, resolved_id),
            **job,
        }


def _matches_keywords(job: dict, keywords: object) -> bool:
    """Клиентский фильтр: любое слово есть в заголовке/описании/локации."""
    if not keywords:
        return True
    if isinstance(keywords, str):
        keywords = [keywords]
    terms = [str(term).strip().casefold() for term in keywords if str(term).strip()]
    if not terms:
        return True
    haystack_parts = [
        str(job.get("title") or ""),
        str(job.get("location") or ""),
        str(job.get("content") or ""),
        str(job.get("description_raw") or ""),
        str(job.get("description_html") or ""),
    ]
    haystack = _strip_tags(" ".join(haystack_parts)) or ""
    haystack = haystack.casefold()
    return any(term in haystack for term in terms)
