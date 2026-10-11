"""Ashby Job Board source adapter (docs/04_PARSING_RULES.md §10).

Площадка — публичные доски вакансий Ashby (``job_board_name`` компании,
например ``Ashby``, ``Linear``). Два пути получения данных:

- **Основной** — публичный Posting API
  ``https://api.ashbyhq.com/posting-api/job-board/{job_board_name}?includeCompensation=true``
  (без ключа): JSON ``{"jobs": [...]}`` с полями ``id`` (UUID), ``title``,
  ``location``, ``employmentType``, ``workplaceType``, ``descriptionHtml``,
  ``publishedAt``, ``jobUrl`` и объектом ``compensation``
  (``summaryComponents``/``compensationTiers[].components[]`` с числовой
  зарплатой). Запрос идёт через общую ``AntiBanSession`` оркестратора.
- **Fallback** — при отказе/непригодности API (5xx, 404, не-JSON, сетевая
  ошибка) HTML-разбор страницы ``https://jobs.ashbyhq.com/{job_board_name}``.
  Доска — SPA: вакансии лежат в hydration-JSON ``window.__appData`` с ключом
  ``jobBoard.jobPostings`` (плюс ``jobBoard.teams`` и ``organization.name``),
  а карточка ``https://jobs.ashbyhq.com/{board}/{id}`` — в ``posting``.

У Ashby **нет** отдельного endpoint'а одной вакансии, поэтому ``get_vacancy``
сначала ищет id в полном ответе Posting API, а при неудаче разбирает
HTML-карточку. Поиск ``search`` фильтрует ``isListed=false`` (вакансии,
доступные только по прямой ссылке) и применяет клиентский фильтр ``keywords``.

Нормализация (``normalize``) приводит сырые данные к канонической схеме
docs/04 §7; зарплата берётся из структурированного ``compensation`` (API) либо
парсится из строки ``scrapeableCompensationSalarySummary``/``compensationTierSummary``
(HTML).

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
    "PRESET_JOB_BOARDS",
    "PRESET_COMPANY_NAMES",
    "AshbyAdapter",
    "AshbyAPIError",
    "api_url_for",
    "company_name_for",
    "extract_card_fields",
    "extract_salary_range",
    "has_next_page",
    "job_url_for",
    "listing_url_for",
    "normalize_job_board",
    "parse_api_jobs",
    "parse_html_jobs",
]

logger = logging.getLogger(__name__)

#: Публичный Posting API Ashby (без ключа).
API_BASE = "https://api.ashbyhq.com/posting-api/job-board"
#: Страницы выдачи доски — цель HTML-fallback при отказе API.
LISTING_BASE = "https://jobs.ashbyhq.com"

#: Пресет реальных досок Ashby: используется, когда ``job_board_name`` не задан.
PRESET_JOB_BOARDS: tuple[str, ...] = (
    "Ashby",
    "Linear",
    "ramp",
)

#: Отображаемое имя компании для досок пресета (фолбэк company_name).
PRESET_COMPANY_NAMES: dict[str, str] = {
    "Ashby": "Ashby",
    "Linear": "Linear",
    "ramp": "Ramp",
}

#: Разрешённые хосты ссылок API/выдачи/карточек (docs/04 §4.2 — белый список).
_ALLOWED_HOSTS = frozenset(
    {
        "api.ashbyhq.com",
        "jobs.ashbyhq.com",
        "www.jobs.ashbyhq.com",
    }
)
#: Статусы «вакансия удалена» (docs/04 §5 → get_vacancy вернёт None).
_NOT_FOUND_STATUSES = (404, 410)

#: UUID вакансии Ashby (внешний id).
_UUID_PATTERN = (
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
_UUID_RE = re.compile(_UUID_PATTERN)

#: Мета-теги карточки (фолбэк заголовка, если hydration не найден).
_OG_TITLE_RE = re.compile(
    r'<meta[^>]*property=["\']og:title["\'][^>]*content=["\'](?P<text>[^"\']*)["\']',
    re.IGNORECASE,
)
_TITLE_RE = re.compile(r"<title[^>]*>(?P<text>.*?)</title>", re.IGNORECASE | re.DOTALL)

#: Формы занятости Ashby (``employmentType`` enum) → канонические подписи.
_EMPLOYMENT_FORM_MAP: dict[str, str] = {
    "fulltime": "Full-time",
    "parttime": "Part-time",
    "intern": "Intern",
    "contract": "Contract",
    "temporary": "Temporary",
}


class AshbyAPIError(RuntimeError):
    """Ошибка Posting API Ashby (не-JSON, 4xx/5xx, сетевой сбой)."""


# --- URL и вспомогательные функции -------------------------------------------


def normalize_job_board(value: object) -> str | None:
    """Нормализовать ``job_board_name``: обрезка, слэши, валидация символов.

    Returns:
        str | None: чистое имя доски либо None для пустого значения.

    Raises:
        ValueError: недопустимые символы (имя участвует в построении URL).
    """
    if value is None:
        return None
    board = str(value).strip().strip("/")
    if not board:
        return None
    if not re.fullmatch(r"[A-Za-z0-9._\-]+", board):
        raise ValueError(f"Некорректный Ashby job board name: {value!r}")
    return board


def api_url_for(job_board: str) -> str:
    """Ссылка Posting API доски (``includeCompensation`` — зарплаты в JSON)."""
    return f"{API_BASE}/{job_board}?includeCompensation=true"


def listing_url_for(job_board: str) -> str:
    """Страница выдачи доски — цель HTML-fallback."""
    return f"{LISTING_BASE}/{job_board}"


def job_url_for(job_board: str, external_id: str) -> str:
    """Прямая ссылка карточки вакансии доски."""
    return f"{LISTING_BASE}/{job_board}/{external_id}"


def company_name_for(job_board: str | None) -> str | None:
    """Отображаемое имя компании для доски пресета (иначе None)."""
    if not job_board:
        return None
    return PRESET_COMPANY_NAMES.get(job_board)


def _is_api_url(url: str) -> bool:
    """Правда, если ссылка указывает на Posting API Ashby."""
    return "api.ashbyhq.com" in (url or "")


def _board_from_url(url: object) -> str | None:
    """``job_board_name`` из ссылки API/выдачи/карточки (иначе None)."""
    value = _as_text(url)
    if not value or "ashbyhq.com" not in value:
        return None
    parts = [part for part in urlsplit(value).path.split("/") if part]
    if not parts:
        return None
    if "job-board" in parts:  # /posting-api/job-board/<board>?...
        index = parts.index("job-board")
        candidate = parts[index + 1] if index + 1 < len(parts) else None
    else:  # jobs.ashbyhq.com/<board>[/<id>]
        candidate = parts[0]
    try:
        return normalize_job_board(candidate)
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


def _to_int(value: object) -> int | None:
    """Число → int (округление); не-число → None."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return None


def _strip_tags(fragment: str) -> str | None:
    """HTML → плоский текст (сущности раскрываются, пробелы схлопываются)."""
    text = re.sub(r"<[^>]+>", " ", fragment)
    text = html_lib.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text or None


# --- разбор API --------------------------------------------------------------


def parse_api_jobs(text: str) -> list[dict]:
    """Разбор ответа ``.../job-board/{board}``: ``{"jobs": [...]}``.

    Returns:
        list[dict]: вакансии с непустым ``id`` (UUID) в порядке выдачи.

    Raises:
        ValueError: не-JSON-объект или отсутствует список ``jobs``.
        json.JSONDecodeError: не-JSON тело ответа.
    """
    data = json.loads(text)
    if not isinstance(data, dict) or not isinstance(data.get("jobs"), list):
        raise ValueError("Ashby API: в ответе нет списка «jobs»")
    return [
        job
        for job in data["jobs"]
        if isinstance(job, dict) and job.get("id") not in (None, "")
    ]


# --- разбор HTML (fallback) ---------------------------------------------------


def _extract_payload(html: str) -> dict | None:
    """Hydration-JSON ``window.__appData`` из страницы Ashby (выдача/карточка).

    Страницы Ashby — SPA: данные вакансий отдаются клиенту одним JSON-объектом
    в ``<script> window.__appData = {...} </script>``. На выдаче это объект
    с ``jobBoard``/``organization``, на карточке — с ``posting``.

    Returns:
        dict | None: разобранный объект либо None, если hydration не найден.
    """
    marker = html.find("__appData")
    if marker >= 0:
        brace = html.find("{", marker)
        if brace >= 0:
            try:
                data, _ = json.JSONDecoder().raw_decode(html[brace:])
            except ValueError:
                data = None
            if isinstance(data, dict):
                return data
    # Запасной путь: любой <script> с ключами гидратации (без window.__appData).
    for match in re.finditer(r"<script[^>]*>(.*?)</script>", html, re.S | re.I):
        body = match.group(1)
        if '"jobBoard"' not in body and '"posting"' not in body:
            continue
        brace = body.find("{")
        if brace < 0:
            continue
        try:
            data, _ = json.JSONDecoder().raw_decode(body[brace:])
        except ValueError:
            continue
        if isinstance(data, dict) and (
            "jobBoard" in data or "posting" in data or "organization" in data
        ):
            return data
    return None


def parse_html_jobs(html: str, job_board: str | None = None) -> list[dict]:
    """Разобрать страницу выдачи ``jobs.ashbyhq.com/{board}`` (SPA hydration).

    Извлекает ``jobBoard.jobPostings`` (id, title, locationName, workplaceType,
    employmentType, compensationTierSummary), имена команд из ``jobBoard.teams``
    и компанию из ``organization.name``.

    Returns:
        list[dict]: карточки ``{id, external_id, title, location, workplaceType,
        employmentType, team?, compensationTierSummary?, url?, company_name?}``
        в порядке выдачи, без дублей id.
    """
    payload = _extract_payload(html)
    if not payload:
        return []

    organization = payload.get("organization")
    organization = organization if isinstance(organization, dict) else {}
    company_name = _as_text(organization.get("name"), limit=512)
    board = _as_text(organization.get("hostedJobsPageSlug")) or job_board

    job_board_data = payload.get("jobBoard")
    job_board_data = job_board_data if isinstance(job_board_data, dict) else {}
    teams: dict[str, str] = {}
    for team in job_board_data.get("teams") or []:
        if isinstance(team, dict) and team.get("id"):
            teams[team["id"]] = team.get("name")

    jobs: list[dict] = []
    seen: set[str] = set()
    for posting in job_board_data.get("jobPostings") or []:
        if not isinstance(posting, dict):
            continue
        external_id = _as_text(posting.get("id"))
        if not external_id or external_id in seen:
            continue
        seen.add(external_id)

        card: dict[str, Any] = {
            "id": external_id,
            "external_id": external_id,
            "title": _as_text(posting.get("title")),
            "location": _as_text(posting.get("locationName")),
            "workplaceType": posting.get("workplaceType"),
            "employmentType": posting.get("employmentType"),
        }
        if posting.get("compensationTierSummary"):
            card["compensationTierSummary"] = posting.get("compensationTierSummary")
        team = teams.get(posting.get("teamId"))
        if team:
            card["team"] = team
        if board:
            card["url"] = job_url_for(board, external_id)
        if company_name:
            card["company_name"] = company_name
        jobs.append(card)
    return jobs


def has_next_page(html: str) -> bool:
    """Признак следующей страницы выдачи.

    Доски Ashby отдают все вакансии одной страницей (API — одним ответом,
    HTML — одним hydration-JSON), поэтому пагинации нет.
    """
    return False


# --- зарплата -----------------------------------------------------------------


def extract_salary_range(value: object) -> tuple[int | None, int | None, str | None]:
    """Разобрать строку зарплаты Ashby (``$120K – $165K • Offers Equity``).

    Обрезается по ``•``/``·``/``|`` (бонусы и equity не входят в диапазон),
    тире (``–``/``—``/``−``) нормализуются в дефис, символ валюты
    (``$``/``€``/``£``) определяет ``currency``, суффикс ``K`` умножает на 1000.

    Returns:
        tuple: ``(salary_from, salary_to, salary_currency)``; для одной суммы
        обе границы равны, если нечего разобрать — все None.
    """
    if value is None:
        return None, None, None
    text = str(value)
    for separator in ("•", "·", "|"):
        index = text.find(separator)
        if index > 0:
            text = text[:index]

    currency = None
    if "$" in text:
        currency = "USD"
    elif "€" in text:
        currency = "EUR"
    elif "£" in text:
        currency = "GBP"

    text = text.replace("–", "-").replace("—", "-").replace("−", "-")
    for symbol in ("$", "€", "£"):
        text = text.replace(symbol, " ")

    amounts: list[int] = []
    for number, thousands in re.findall(r"(\d[\d,]*(?:\.\d+)?)\s*([kK])?", text):
        try:
            parsed = float(number.replace(",", ""))
        except ValueError:
            continue
        if thousands:
            parsed *= 1000
        amounts.append(int(round(parsed)))

    if not amounts:
        return None, None, currency
    if len(amounts) == 1:
        return amounts[0], amounts[0], currency
    return amounts[0], amounts[1], currency


def _salary_from_compensation(
    compensation: object,
) -> tuple[int | None, int | None, str | None]:
    """Числовая зарплата из структурированного ``compensation`` Posting API.

    Берётся компонент ``compensationType == "Salary"`` из ``summaryComponents``
    (приоритет) либо из ``compensationTiers[].components[]``.
    """
    if not isinstance(compensation, dict):
        return None, None, None

    def scan(components: object) -> tuple[int | None, int | None, str | None]:
        for component in components or []:
            if not isinstance(component, dict):
                continue
            if str(component.get("compensationType") or "").casefold() != "salary":
                continue
            return (
                _to_int(component.get("minValue")),
                _to_int(component.get("maxValue")),
                _as_text(component.get("currencyCode"), limit=8),
            )
        return None, None, None

    salary = scan(compensation.get("summaryComponents"))
    if salary[0] is not None or salary[1] is not None:
        return salary

    tiers = compensation.get("compensationTiers")
    if isinstance(tiers, list):
        for tier in tiers:
            if isinstance(tier, dict):
                salary = scan(tier.get("components"))
                if salary[0] is not None or salary[1] is not None:
                    return salary
    return None, None, None


# --- нормализация значений ----------------------------------------------------


def _normalize_work_format(value: object, is_remote: object = None) -> str | None:
    """``workplaceType`` Ashby (OnSite/Remote/Hybrid) → канонический ``work_format``."""
    text = _as_text(value, limit=64)
    if not text:
        return "remote" if is_remote else None
    lowered = text.casefold().replace(" ", "").replace("-", "")
    if lowered == "remote":
        return "remote"
    if lowered == "hybrid":
        return "hybrid"
    if lowered in ("onsite", "office"):
        return "onsite"
    return "remote" if is_remote else text


def _normalize_employment_form(value: object) -> str | None:
    """``employmentType`` enum Ashby (FullTime/...) → подписанная форма."""
    text = _as_text(value, limit=64)
    if not text:
        return None
    key = text.casefold().replace(" ", "").replace("-", "").replace("_", "")
    return _EMPLOYMENT_FORM_MAP.get(key, text)


def _matches_keywords(job: dict, keywords: object) -> bool:
    """Клиентский фильтр: любое слово есть в заголовке/локации/описании."""
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
        str(job.get("locationName") or ""),
        str(job.get("department") or ""),
        str(job.get("team") or ""),
        str(job.get("descriptionPlain") or ""),
        str(job.get("descriptionHtml") or ""),
    ]
    haystack = _strip_tags(" ".join(haystack_parts)) or ""
    haystack = haystack.casefold()
    return any(term in haystack for term in terms)


# --- разбор карточки ----------------------------------------------------------


def extract_card_fields(html: str) -> dict:
    """Поля вакансии из HTML карточки ``jobs.ashbyhq.com/{board}/{id}``.

    Источник — hydration ``window.__appData.posting`` (структурированный JSON
    внутри HTML): заголовок, локация, формат работы, описание и зарплата.
    Компания берётся из ``organization.name``; заголовок при отсутствии
    ``posting`` — из ``og:title``/``<title>``.

    Returns:
        dict: контент-поля vacancies (docs/02 §3.3): ``title``,
        ``company_name``, ``area``, ``employment_form``, ``work_format``,
        ``salary_from/to/currency``, ``description_raw/html`` — только непустые.
    """
    fields: dict[str, Any] = {}

    def fill(key: str, value: Any) -> None:
        if value is None or value == "":
            return
        fields.setdefault(key, value)

    payload = _extract_payload(html)
    posting = payload.get("posting") if isinstance(payload, dict) else None
    organization = payload.get("organization") if isinstance(payload, dict) else None

    if isinstance(posting, dict):
        fill("title", _as_text(posting.get("title"), limit=512))
        fill("area", _as_text(posting.get("locationName"), limit=255))
        fill("work_format", _normalize_work_format(posting.get("workplaceType")))
        fill("employment_form", _normalize_employment_form(posting.get("employmentType")))

        description_html = _as_text(posting.get("descriptionHtml"))
        if description_html:
            fill("description_html", description_html)
            fill("description_raw", _strip_tags(description_html))

        salary_text = _as_text(
            posting.get("scrapeableCompensationSalarySummary")
            or posting.get("compensationTierSummary")
        )
        if salary_text:
            salary_from, salary_to, salary_currency = extract_salary_range(salary_text)
            fill("salary_from", salary_from)
            fill("salary_to", salary_to)
            fill("salary_currency", salary_currency)

    if isinstance(organization, dict):
        fill("company_name", _as_text(organization.get("name"), limit=512))

    if not fields.get("title"):
        og_title = _OG_TITLE_RE.search(html)
        if og_title:
            fill("title", _as_text(html_lib.unescape(og_title.group("text")), limit=512))
        else:
            title_element = _TITLE_RE.search(html)
            if title_element:
                fill("title", _strip_tags(title_element.group("text")))

    return fields


# --- адаптер -----------------------------------------------------------------


class AshbyAdapter(BaseSourceAdapter):
    """Источник Ashby: Posting API + HTML-hydration fallback (docs/04 §10)."""

    #: Имя источника в SourceRegistry и в vacancies.source.
    source_name = "ashby"

    def __init__(
        self,
        session: AntiBanSession | None = None,
        job_board: str | None = None,
    ) -> None:
        # Сессия та же, что у оркестратора: единые прогрев/паузы/прокси и
        # общая статистика трафика (docs/04 §3).
        self.http = session or AntiBanSession(fetcher=build_page_fetcher())
        # Доска по умолчанию: явное имя либо первый элемент пресета.
        self.job_board = normalize_job_board(job_board) or PRESET_JOB_BOARDS[0]

    # --- контракт BaseSourceAdapter (docs/04 §10.1) --------------------------

    async def search(self, filters: dict) -> list[dict]:
        """Собрать карточки выдачи доски: API → HTML-hydration (docs/04 §4.1–§4.2).

        Args:
            filters: ``job_board`` (доска), ``search_url`` (готовая ссылка
                выдачи, §4.2), ``keywords`` (клиентский фильтр — Posting API не
                поддерживает текстовый поиск), ``limit``, ``max_pages``
                (по умолчанию 1: API отдаёт все вакансии разом, HTML-фолбэк
                тоже одностраничный).

        Returns:
            list[dict]: сырые карточки ``{source, external_id, url, ...}``
            в порядке выдачи, без дублей; вакансии ``isListed=false``
            (доступны только по прямой ссылке) отсеиваются.
        """
        search_url = filters.get("search_url")
        job_board = normalize_job_board(filters.get("job_board")) or self.job_board
        max_pages = max(1, int(filters.get("max_pages") or 1))
        keywords = filters.get("keywords")
        limit = filters.get("limit")

        if search_url:
            base_url = self.validate_search_url(str(search_url))
            job_board = _board_from_url(base_url) or job_board
        else:
            base_url = api_url_for(job_board)

        results: list[dict] = []
        seen: set[str] = set()
        for page_index in range(max_pages):
            try:
                jobs, has_next = await self._fetch_page(base_url, job_board, page_index)
            except Exception as exc:  # noqa: BLE001 — страница не роняет сбор
                logger.warning(
                    "Ashby: не удалось получить страницу %s выдачи %s: %s",
                    page_index + 1,
                    base_url,
                    exc,
                )
                break

            for job in jobs:
                external_id = str(job.get("id") or job.get("external_id") or "").strip()
                if not external_id or external_id in seen:
                    continue
                if job.get("isListed") is False:
                    continue  # прямая ссылка, не публикуется в выдаче
                if not _matches_keywords(job, keywords):
                    continue
                seen.add(external_id)
                results.append(self._raw_card(job, base_url, job_board, external_id))

            if not has_next:
                break  # API и HTML отдают все вакансии одной страницей

        if limit is not None:
            results = results[: max(0, int(limit))]
        return results

    async def get_vacancy(self, external_id_or_url: str) -> dict | None:
        """Одна вакансия по id или ссылке (docs/04 §5).

        У Ashby нет endpoint'а одной вакансии, поэтому основной путь — полный
        ответ Posting API с поиском по ``id``; при отказе/отсутствии —
        HTML-карточка ``jobs.ashbyhq.com/{board}/{id}``.

        Returns:
            dict | None: сырые данные вакансии либо None, если вакансия
            не найдена/удалена на источнике (docs/04 §5 → not_found).
        """
        resolved = self._resolve_job(external_id_or_url)
        if not resolved:
            return None
        job_board, external_id = resolved

        # 1) Основной путь: Posting API → поиск по id.
        try:
            response = await self.http.fetch(api_url_for(job_board))
            if response.status_code == 200:
                for job in parse_api_jobs(response.text):
                    if str(job.get("id")) == external_id:
                        return self._raw_job(job, job_board, external_id)
            raise AshbyAPIError(
                f"Ashby API не содержит вакансию {external_id} "
                f"(HTTP {response.status_code})"
            )
        except Exception as exc:  # noqa: BLE001 — любой сбой → HTML-fallback
            logger.warning(
                "Ashby API недоступен для вакансии %s, пробую HTML: %s",
                external_id,
                exc,
            )

        # 2) Fallback: карточка в HTML.
        card_url = job_url_for(job_board, external_id)
        try:
            response = await self.http.fetch(card_url)
        except Exception as exc:  # noqa: BLE001
            logger.debug("Ashby: HTML-карточка недоступна: %s", exc)
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

        url = _as_text(raw.get("jobUrl") or raw.get("url"))
        job_board = _board_from_url(url) or self.job_board
        if not url:
            url = job_url_for(job_board, external_id)

        # Локация: API → строка ``location`` (или ``address``), HTML → ``area``.
        location = raw.get("area") or raw.get("location") or raw.get("locationName")
        if isinstance(location, dict):
            location = location.get("name")
        if not location:
            address = raw.get("address")
            if isinstance(address, dict):
                postal = address.get("postalAddress")
                postal = postal if isinstance(postal, dict) else address
                parts = [
                    postal.get("addressLocality"),
                    postal.get("addressRegion"),
                    postal.get("addressCountry"),
                ]
                location = ", ".join(part for part in parts if part) or None
        if isinstance(location, (list, tuple)):
            location = ", ".join(str(item) for item in location if item)
        area = _as_text(location, limit=255) or ""

        description_html = _as_text(
            raw.get("description_html") or raw.get("descriptionHtml")
        )
        description_raw = _as_text(
            raw.get("description_raw") or raw.get("descriptionPlain")
        )
        if not description_raw and description_html:
            description_raw = _strip_tags(description_html)

        work_format = _normalize_work_format(
            raw.get("work_format") or raw.get("workplaceType"),
            raw.get("isRemote"),
        )
        if not work_format and "remote" in area.casefold():
            work_format = "remote"

        employment_form = _normalize_employment_form(
            raw.get("employment_form") or raw.get("employmentType")
        )

        # Зарплата: готовые поля (HTML-карточка) → структурированный
        # compensation (API) → строковый summary.
        salary_from = _to_int(raw.get("salary_from"))
        salary_to = _to_int(raw.get("salary_to"))
        salary_currency = _as_text(raw.get("salary_currency"), limit=8)
        if salary_from is None and salary_to is None:
            compensation = raw.get("compensation")
            if isinstance(compensation, dict):
                salary_from, salary_to, salary_currency = _salary_from_compensation(
                    compensation
                )
        if salary_from is None and salary_to is None:
            salary_text = _as_text(
                raw.get("scrapeableCompensationSalarySummary")
                or raw.get("compensationTierSummary")
            )
            if salary_text:
                salary_from, salary_to, salary_currency = extract_salary_range(salary_text)

        return {
            "source": self.source_name,
            "external_id": external_id,
            "url": url,
            "title": _as_text(raw.get("title"), limit=512),
            "company_name": _as_text(raw.get("company_name"), limit=512)
            or company_name_for(job_board),
            "salary_from": salary_from,
            "salary_to": salary_to,
            "salary_currency": salary_currency,
            "experience": _as_text(raw.get("experience"), limit=64),
            "employment_form": employment_form,
            "work_format": work_format,
            "schedule": _as_text(raw.get("schedule"), limit=128),
            "area": area,
            "published_at": _as_text(raw.get("publishedAt") or raw.get("published_at")),
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
        city: str | None = None,
        job_board: str | None = None,
        search_url: str | None = None,
        **_kwargs: object,
    ) -> str:
        """Ссылка выдачи автопоиска — Posting API доски (docs/04 §4.1).

        Ashby API не принимает текстовый запрос и фильтры: ``keywords`` и
        остальные фильтры автопоиска применяются на клиенте в :meth:`search`.
        """
        if search_url:
            return self.validate_search_url(str(search_url))
        board = normalize_job_board(job_board) or self.job_board
        return api_url_for(board)

    def validate_search_url(self, search_url: str) -> str:
        """Принимаются только ссылки API/выдачи Ashby (docs/04 §4.2)."""
        candidate = str(search_url).strip()
        parts = urlsplit(candidate)
        host = (parts.hostname or "").lower()
        if parts.scheme not in ("http", "https") or host not in _ALLOWED_HOSTS:
            raise ValueError(
                "Ссылка выдачи Ashby должна быть на api.ashbyhq.com или "
                f"jobs.ashbyhq.com: {candidate!r}"
            )
        return candidate

    def page_url(self, base_url: str, page: int) -> str:
        """Страница выдачи: Ashby отдаёт все вакансии одной страницей.

        Posting API возвращает полный список разом, HTML-выдача — SPA с одним
        hydration-JSON, поэтому пагинация ``?page=N`` отсутствует.
        """
        return base_url

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
        """Прямая ссылка карточки: ``jobs.ashbyhq.com/{board}/{id}``."""
        external_id = str(external_id).strip()
        if not external_id:
            return base_url
        job_board = _board_from_url(base_url) or self.job_board
        return job_url_for(job_board, external_id)

    def extract_fields(self, html: str) -> dict:
        """Извлечь поля вакансии из HTML карточки (docs/04 §7)."""
        return extract_card_fields(html)

    # --- вспомогательное -----------------------------------------------------

    async def _fetch_page(
        self, base_url: str, job_board: str, page_index: int
    ) -> tuple[list[dict], bool]:
        """Одна страница выдачи: основной путь API → fallback HTML."""
        if _is_api_url(base_url):
            try:
                response = await self.http.fetch(base_url)
                if response.status_code != 200:
                    raise AshbyAPIError(
                        f"Ashby API вернул {response.status_code}: {base_url}"
                    )
                return parse_api_jobs(response.text), False
            except Exception as exc:  # noqa: BLE001 — любая ошибка → HTML-fallback
                logger.warning(
                    "Ashby API недоступен (%s), переключаюсь на HTML-выдачу %s",
                    exc,
                    listing_url_for(job_board),
                )
            url = self.page_url(listing_url_for(job_board), page_index)
        else:
            url = self.page_url(base_url, page_index)

        response = await self.http.fetch(url)
        if response.status_code == 200:
            return parse_html_jobs(response.text, job_board), has_next_page(response.text)
        if response.status_code in _NOT_FOUND_STATUSES:
            return [], False
        raise AshbyAPIError(f"Ashby HTML-выдача вернула {response.status_code}: {url}")

    def _resolve_job(self, external_id_or_url: str) -> tuple[str, str] | None:
        """``(job_board, external_id)`` из id или прямой ссылки (иначе None)."""
        value = (external_id_or_url or "").strip()
        if not value:
            return None
        if value.startswith(("http://", "https://")):
            parts = [part for part in urlsplit(value).path.split("/") if part]
            if not parts or "job-board" in parts:
                return None  # ссылка выдачи, а не карточки
            if len(parts) < 2:
                return None  # страница доски без id
            try:
                job_board = normalize_job_board(parts[0])
            except ValueError:
                return None
            external_id = parts[1]
        else:
            job_board, external_id = self.job_board, value
        if not job_board or not external_id or not _UUID_RE.fullmatch(external_id):
            return None
        return job_board, external_id

    @staticmethod
    def _raw_job(job: dict, job_board: str, external_id: str) -> dict:
        """Сырая карточка вакансии в формате search/get_vacancy."""
        resolved_id = str(job.get("id") or external_id)
        return {
            "source": AshbyAdapter.source_name,
            "external_id": resolved_id,
            "url": _as_text(job.get("jobUrl") or job.get("url"))
            or job_url_for(job_board, resolved_id),
            **job,
        }

    def _raw_card(
        self, job: dict, base_url: str, job_board: str, external_id: str
    ) -> dict:
        """Сырая карточка выдачи c источником, id, ссылкой и компанией."""
        card = dict(job)
        card["source"] = self.source_name
        card["external_id"] = external_id
        card["url"] = (
            _as_text(job.get("jobUrl") or job.get("url"))
            or self.build_vacancy_url(base_url, external_id)
        )
        if not card.get("company_name"):
            card["company_name"] = company_name_for(job_board)
        return card
