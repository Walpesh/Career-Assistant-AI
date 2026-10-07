"""Remotive source adapter (docs/04_PARSING_RULES.md §10).

Площадка удалённой работы remotive.com. Два пути получения данных:

- **Основной** — публичный API ``https://remotive.com/api/remote-jobs``
  (без ключа): JSON-массив вакансий; первые два элемента — предупреждения
  и юридическое уведомление, они пропускаются. Запрос идёт через общую
  ``AntiBanSession`` оркестратора.
- **Fallback** — при отказе/троттлинге API (429, не-JSON, сетевая ошибка)
  лёгкий HTML-парсинг главной страницы ``remotive.com`` через ``curl_cffi``
  (docs/04 §2 п.1): из разметки извлекаются ссылки вакансий.

Нормализация (``normalize``) приводит сырые данные к канонической схеме
docs/04 §7 (ключи ``source``, ``external_id``, ``url``, ``title``,
``company_name``, ``salary_from/to/currency``, ``experience``,
``employment_form``, ``work_format``, ``schedule``, ``area``,
``published_at``, ``description_raw``, ``description_html``).

Anti-ban (прогрев, паузы 4–8 с, ротация прокси) остаётся в Proxy &
Anti-Ban Module: API и карточки запрашиваются через готовую
``AntiBanSession``; curl_cffi-fallback — исключение (одиночный лёгкий
GET, см. ``_fetch_html``).
"""

from __future__ import annotations

import html as html_lib
import json
import re
from typing import Any
from urllib.parse import urlsplit

from app.modules.anti_ban import AntiBanSession
from app.modules.parsing.fetcher import build_page_fetcher
from app.modules.parsing.sources.base import BaseSourceAdapter

__all__ = [
    "API_URL",
    "HOME_URL",
    "LISTING_URL",
    "RemotiveAdapter",
    "RemotiveAPIError",
    "extract_salary_range",
    "parse_html_jobs",
]

logger = __import__("logging").getLogger(__name__)

#: Публичный API Remotive (без ключа); первые два элемента массива —
#: ``00-warning`` и ``0-legal-notice``, они пропускаются.
API_URL = "https://remotive.com/api/remote-jobs"
#: Главная страница — цель HTML-fallback при отказе/троттлинге API.
HOME_URL = "https://remotive.com"
#: Лента вакансий (хуки оркестратора и валидация ссылок выдачи).
LISTING_URL = "https://remotive.com/remote-jobs"

#: Разрешённые хосты ссылок выдачи (docs/04 §4.2 — белый список доменов).
_ALLOWED_HOSTS = ("remotive.com", "www.remotive.com")
#: Статусы «вакансия удалена» (docs/04 §5 → get_vacancy вернёт None).
_NOT_FOUND_STATUSES = (404, 410)

#: Якорь карточки: <a ... href=\"/remote-jobs/<category>/<slug>-<id>\" ...>заголовок</a>.
_ANCHOR_RE = re.compile(
    r"<a\b(?P<pre>[^>]*?)\s+href=[\"'](?:(?:https?:)?//(?:www\.)?remotive\.com)?/"
    r"remote-jobs/(?P<category>[^/]+)/(?P<slug>[^\"'#?]+)-(?P<external_id>\d+)[\"'](?P<post>[^>]*)>(?P<title>.*?)</a>",
    re.IGNORECASE | re.DOTALL,
)

#: Ссылка пагинации «следующая страница» (признак has_next в parse_listing).
_NEXT_LINK_RE = re.compile(
    r"<a\b[^>]*\srel=[\"'][^\"']*\bnext\b[^\"']*[\"'][^>]*>", re.IGNORECASE
)

#: Числовой идентификатор в конце слага: «...-111111» → «111111».
_TRAILING_ID_RE = re.compile(r"(\d+)$")


class RemotiveAPIError(RuntimeError):
    """Ошибка API Remotive (не-JSON, 429, сетевой сбой)."""


def extract_salary_range(salary_str: str | None) -> tuple[int | None, int | None, str | None]:
    """Extract salary range and currency from Remotive salary string.
    
    Examples:
        "$20k - $35k" -> (20000, 35000, "USD")
        "$60,000 - $80,000" -> (60000, 80000, "USD")
        "€3000" -> (3000, None, "EUR")
        None -> (None, None, None)
    """
    if not salary_str:
        return None, None, None
    
    # Clean the string
    salary_str = salary_str.strip()
    
    # Currency detection
    currency = None
    if salary_str.startswith("$"):
        currency = "USD"
    elif salary_str.startswith("€"):
        currency = "EUR"
    elif salary_str.startswith("£"):
        currency = "GBP"
    elif salary_str.startswith("₪"):
        currency = "ILS"
    
    # Remove currency symbol and spaces
    clean_str = salary_str
    for symbol in ["$", "€", "£", "₪"]:
        clean_str = clean_str.replace(symbol, "")
    clean_str = clean_str.strip()
    
    # Handle "k" notation (thousands)
    def parse_amount(amount_str: str) -> int | None:
        amount_str = amount_str.strip()
        if not amount_str:
            return None
        multiplier = 1
        if amount_str.endswith("k") or amount_str.endswith("K"):
            multiplier = 1000
            amount_str = amount_str[:-1]
        try:
            # Remove commas and parse
            amount_str = amount_str.replace(",", "")
            return int(float(amount_str) * multiplier)
        except ValueError:
            return None
    
    # Split by dash or hyphen
    if "-" in clean_str:
        parts = clean_str.split("-", 1)
        min_salary = parse_amount(parts[0]) if parts[0].strip() else None
        max_salary = parse_amount(parts[1]) if len(parts) > 1 and parts[1].strip() else None
        return min_salary, max_salary, currency
    else:
        # Single value
        amount = parse_amount(clean_str)
        return amount, amount, currency


def _as_text(value: object, *, limit: int | None = None) -> str | None:
    """Строковое значение: None/пусто → None, длинное — обрезается."""
    if value is None:
        return None
    result = str(value).strip()
    if not result:
        return None
    return result[:limit] if limit else result


def parse_html_jobs(html: str) -> list[dict[str, Any]]:
    """Parse job listings from HTML fallback.
    
    Returns list of dicts with keys: id, title, company_name, location, tags, etc.
    """
    jobs = []
    
    for match in _ANCHOR_RE.finditer(html):
        external_id = match.group("external_id")
        title = html_lib.unescape(match.group("title").strip())
        
        # Extract context around the match for data attributes
        start = max(0, match.start() - 1000)
        end = min(len(html), match.end() + 1000)
        context = html[start:end]
        
        # Extract data attributes from the context
        company_name = None
        location = None
        tags = []
        salary_str = None
        
        # Look for data attributes in the anchor's pre/post context
        pre_context = match.group("pre")
        post_context = match.group("post")
        
        # Simple extraction from data-attributes if present
        for attr_name, var_name in [("data-company", "company_name"), 
                                    ("data-location", "location"),
                                    ("data-tags", "tags"),
                                    ("data-salary", "salary_str")]:
            pattern = rf'{attr_name}=["\']([^"\']*)["\']'
            for context_part in [pre_context, post_context]:
                match_attr = re.search(pattern, context_part, re.IGNORECASE)
                if match_attr:
                    value = match_attr.group(1).strip()
                    if var_name == "tags":
                        tags = [tag.strip() for tag in value.split(",") if tag.strip()]
                    elif var_name == "salary_str":
                        salary_str = value
                    elif var_name == "company_name":
                        company_name = value
                    elif var_name == "location":
                        location = value
                    break
        
        # If we didn't get company from data-attributes, try to find it nearby
        if not company_name:
            # Look for company name in proximity
            company_patterns = [
                r'data-company=["\']([^"\']*)["\']',
                r'company["\']?\s*[:=]\s*["\']([^"\']*)["\']',
                r'>\s*([^<]+?)\s*<',  # fallback: text between tags
            ]
        
        salary_from, salary_to, salary_currency = extract_salary_range(salary_str)
        
        job = {
            "id": external_id,
            "external_id": external_id,
            "title": title,
            "company_name": company_name,
            "location": location,
            "tags": tags,
            "salary_raw": salary_str,
            "salary_from": salary_from,
            "salary_to": salary_to,
            "salary_currency": salary_currency,
        }
        
        # Only add if we have essential fields
        if job["title"] and job["external_id"]:
            jobs.append(job)
    
    return jobs


class RemotiveAdapter(BaseSourceAdapter):
    """Источник remotive.com: выдача и карточки через AntiBanSession (docs/04 §2, §4)."""

    #: Имя источника в SourceRegistry и в vacancies.source по умолчанию.
    source_name = "remotive"

    def __init__(self, session: AntiBanSession | None = None) -> None:
        # Сессия та же, что у оркестратора: единые прогрев/паузы/прокси и
        # общая статистика трафика (docs/04 §3). Свою создаёт только при
        # прямом использовании адаптера вне оркестратора.
        self.http = session or AntiBanSession(fetcher=build_page_fetcher())

    # --- контракт BaseSourceAdapter (docs/04 §10.1) --------------------------

    async def search(self, filters: dict) -> list[dict]:
        """Собрать карточки выдачи remotive.com с обходом пагинации (docs/04 §4.1–§4.2).

        Args:
            filters: ``search_url`` (готовая ссылка выдачи, §4.2) либо
                ``keywords``/``tags``/``limit`` (§4.1), а также ``max_pages``
                (по умолчанию 1, так как API возвращает все результаты сразу).

        Returns:
            list[dict]: сырые карточки ``{source, external_id, url}``
            в порядке выдачи, без дублей.
        """
        search_url = filters.get("search_url")
        max_pages = max(1, int(filters.get("max_pages") or 1))
        keywords = filters.get("keywords")
        tags = filters.get("tags")
        limit = filters.get("limit")

        if search_url:
            base_url = self.validate_search_url(str(search_url))
        else:
            base_url = self.build_search_url(
                keywords=keywords,
                tags=tags,
                limit=limit,
            )

        results: list[dict] = []
        seen: set[str] = set()

        for page_index in range(max_pages):
            try:
                response = await self.http.fetch(self.page_url(base_url, page_index))
            except Exception as exc:
                logger.warning("Remotive API request failed, trying HTML fallback: %s", exc)
                # Fallback к HTML-странице выдачи (docs/04 §2 п.1)
                try:
                    response = await self.http.fetch(LISTING_URL)
                    raw_jobs = parse_html_jobs(response.text)
                except Exception:
                    # Если и HTML недоступен — пропускаем страницу
                    continue
            else:
                # Try to parse as JSON API
                try:
                    raw_jobs = self._parse_api_response(response.text)
                except (json.JSONDecodeError, KeyError, ValueError) as exc:
                    logger.warning("Remotive API returned invalid JSON, trying HTML fallback: %s", exc)
                    # Fallback to HTML parsing
                    try:
                        raw_jobs = parse_html_jobs(response.text)
                    except Exception:
                        raw_jobs = []

            for job in raw_jobs:
                external_id = str(job.get("id") or job.get("external_id") or "")
                if external_id and external_id not in seen:
                    seen.add(external_id)
                    results.append(
                        {
                            "source": self.source_name,
                            "external_id": external_id,
                            "url": job.get("url") or self.build_vacancy_url(base_url, external_id),
                            **job,
                        }
                    )

            # Check if there's a next page (for HTML fallback)
            if hasattr(response, 'text') and not _NEXT_LINK_RE.search(response.text):
                break

        return results

    async def get_vacancy(self, external_id_or_url: str) -> dict | None:
        """Одна вакансия по внешнему id или прямой ссылке (docs/04 §5).

        Returns:
            dict | None: сырые данные вакансии либо None, если вакансия
            не найдена/удалена на источнике.
        """
        resolved_id = self._resolve_external_id(external_id_or_url)
        if not resolved_id:
            return None

        # Try API first (без параметров: моки и реальный API принимают и
        # ``API_URL``, и ``API_URL?search=...``/``?limit=...``).
        try:
            response = await self.http.fetch(API_URL)
            jobs_data = self._parse_api_response(response.text)
            
            for job in jobs_data:
                if str(job.get("id")) == resolved_id:
                    return {
                        "source": self.source_name,
                        "external_id": str(job.get("id")),
                        "url": job.get("url"),
                        **job,
                    }
        except Exception as exc:
            logger.debug("Remotive API failed for get_vacancy, trying HTML fallback: %s", exc)

        # Fallback to HTML parsing of individual job page
        try:
            job_url = self.build_vacancy_url(LISTING_URL, resolved_id)
            response = await self.http.fetch(job_url)
            if response.status_code in _NOT_FOUND_STATUSES:
                return None
            
            # Extract job details from HTML
            fields = self.extract_fields(response.text)
            if fields:
                return {
                    "source": self.source_name,
                    "external_id": resolved_id,
                    "url": job_url,
                    **fields,
                }
        except Exception as exc:
            logger.debug("Remotive HTML fallback failed for get_vacancy: %s", exc)

        return None

    def normalize(self, raw: dict) -> dict:
        """Привести сырые данные вакансии к канонической схеме (docs/04 §7)."""
        # Handle both API format and HTML fallback format
        external_id = _as_text(raw.get("id") or raw.get("external_id"))
        if not external_id:
            raise ValueError("Vacancy missing external_id")

        # Build URL if not present
        url = _as_text(raw.get("url"))
        if not url:
            url = self.build_vacancy_url(LISTING_URL, external_id)

        # Extract salary information
        salary_from = raw.get("salary_from")
        salary_to = raw.get("salary_to")
        salary_currency = _as_text(raw.get("salary_currency"), limit=8)
        
        # If salary info not normalized yet, try to extract from raw salary
        if salary_from is None and salary_to is None:
            salary_raw = _as_text(raw.get("salary") or raw.get("salary_raw"))
            if salary_raw:
                salary_from, salary_to, salary_currency = extract_salary_range(salary_raw)

        # Handle tags
        tags = raw.get("tags")
        if isinstance(tags, str):
            tags = [tag.strip() for tag in tags.split(",") if tag.strip()]
        elif not isinstance(tags, list):
            tags = []

        return {
            "source": self.source_name,
            "external_id": external_id,
            "url": url,
            "title": _as_text(raw.get("title"), limit=512),
            "company_name": _as_text(raw.get("company_name") or raw.get("company"), limit=512),
            "salary_from": salary_from,
            "salary_to": salary_to,
            "salary_currency": salary_currency,
            "experience": _as_text(raw.get("experience"), limit=64),
            "employment_form": _as_text(raw.get("employment_form"), limit=64),
            "work_format": _as_text(raw.get("work_format")) or "remote",
            "schedule": _as_text(raw.get("schedule"), limit=128),
            "area": _as_text(raw.get("area") or raw.get("location") or raw.get("candidate_required_location"), limit=255) or "",
            "published_at": _as_text(raw.get("publication_date") or raw.get("date") or raw.get("published_at")),
            "description_raw": _as_text(raw.get("description") or raw.get("description_raw")),
            "description_html": _as_text(raw.get("description") or raw.get("description_html")),
            # Remotive-specific fields
            "tags": tags,
            "remote": True,  # All Remotive jobs are remote by definition
        }

    # --- хуки ParsingOrchestrator (docs/04 §10.2) --------------------------

    def build_search_url(
        self,
        *,
        keywords: list[str] | None = None,
        tags: list[str] | None = None,
        limit: int | None = None,
        page: int = 0,
        **_kwargs: object,
    ) -> str:
        """Ссылка выдачи автопоиска (docs/04 §4.1).
        
        Remotive API supports search and limit parameters.
        """
        params = []
        if keywords:
            # Join keywords with space for search
            search_term = " ".join(keywords)
            params.append(f"search={search_term}")
        if tags:
            # Tags parameter - Remotive might not support this directly
            # We'll filter client-side if needed
            pass
        if limit is not None:
            params.append(f"limit={limit}")
        
        base_url = API_URL
        if params:
            return f"{base_url}?{'&'.join(params)}"
        return base_url

    def validate_search_url(self, search_url: str) -> str:
        """Принимаются только ссылки выдачи remotive.com (docs/04 §4.2)."""
        candidate = str(search_url).strip()
        parts = urlsplit(candidate)
        host = (parts.hostname or "").lower()
        if parts.scheme not in ("http", "https") or host not in _ALLOWED_HOSTS:
            raise ValueError(f"Ссылка выдачи Remotive должна быть на remotive.com: {candidate!r}")
        return candidate

    def page_url(self, base_url: str, page: int) -> str:
        """Страница выдачи: Remotive API не использует пагинацию в традиционном смысле,
        но мы поддерживаем параметр limit для контроля количества результатов.
        Для HTML fallback может использоваться традиционная пагинация."""
        # For API, we don't use page parameter - we use limit in build_search_url
        # For HTML fallback, we might need to implement pagination
        if "remotive.com/api" in base_url:
            # API endpoint - ignore page parameter, use limit in build_search_url
            return base_url
        else:
            # HTML fallback - implement traditional pagination
            index = max(0, int(page or 0))
            if index == 0:
                return base_url
            joiner = "&" if "?" in base_url else "?"
            return f"{base_url}{joiner}page={index + 1}"

    def parse_listing(self, html: str) -> tuple[list[str], bool]:
        """Разобрать страницу выдачи: (external_id в порядке выдачи, есть следующая)."""
        jobs = parse_html_jobs(html)
        external_ids = [str(job["id"]) for job in jobs if job.get("id")]
        has_next = bool(_NEXT_LINK_RE.search(html))
        return external_ids, has_next

    def build_vacancy_url(self, base_url: str, external_id: str) -> str:
        """Прямая ссылка карточки: https://remotive.com/remote-jobs/<category>/<slug>-<id>."""
        # Since we don't always have category and slug from API,
        # we construct a URL that Remotive can redirect
        external_id = str(external_id).strip()
        if not external_id:
            return base_url
        
        # Try to construct a realistic URL pattern
        return f"https://remotive.com/remote-jobs/dev-{external_id}"

    def extract_fields(self, html: str) -> dict:
        """Извлечь поля вакансии из HTML карточки (docs/04 §7).

        Приоритет источников: структурные блоки карточки (job-title,
        company-name, location, description, salary, tags) → JSON-LD
        JobPosting → мета-теги → ``<title>`` → разбор выдачи (parse_html_jobs).
        """
        fields: dict[str, Any] = {}

        def fill(key: str, value: Any) -> None:
            """Заполнить поле, только если оно ещё пустое (первый источник главнее)."""
            if value and not fields.get(key):
                fields[key] = value

        def block_text(pattern: str) -> str | None:
            match = re.search(pattern, html, re.IGNORECASE | re.DOTALL)
            if not match:
                return None
            text = re.sub(r"<[^>]+>", " ", match.group(1))
            text = html_lib.unescape(re.sub(r"\s+", " ", text)).strip()
            return text or None

        # 1. Структурные блоки карточки (самый надёжный источник).
        fill("title", block_text(r'<div[^>]*class="[^"]*\bjob-title\b[^"]*"[^>]*>(.*?)</div>'))
        fill("company_name", block_text(r'<div[^>]*class="[^"]*\bcompany-name\b[^"]*"[^>]*>(.*?)</div>'))
        fill("location", block_text(r'<div[^>]*class="[^"]*\blocation\b[^"]*"[^>]*>(.*?)</div>'))

        desc_match = re.search(
            r'<div[^>]*class="[^"]*\bdescription\b[^"]*"[^>]*>(.*?)</div>',
            html,
            re.IGNORECASE | re.DOTALL,
        )
        if desc_match:
            block = desc_match.group(1).strip()
            paragraphs = re.findall(r"<p[^>]*>(.*?)</p>", block, re.IGNORECASE | re.DOTALL)
            source_html = paragraphs[0] if paragraphs else block
            raw_text = html_lib.unescape(
                re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", source_html))
            ).strip()
            fill("description_raw", raw_text or None)
            fill("description_html", block or None)

        salary_text = block_text(r'<div[^>]*class="[^"]*\bsalary\b[^"]*"[^>]*>(.*?)</div>')
        if salary_text:
            salary_from, salary_to, salary_currency = extract_salary_range(salary_text)
            if salary_from is not None or salary_to is not None:
                fill("salary_from", salary_from)
                fill("salary_to", salary_to)
                fill("salary_currency", salary_currency)

        tags = re.findall(
            r'<span[^>]*class="[^"]*\btag\b[^"]*"[^>]*>(.*?)</span>',
            html,
            re.IGNORECASE | re.DOTALL,
        )
        if tags:
            fill(
                "tags",
                [
                    html_lib.unescape(re.sub(r"<[^>]+>", "", tag)).strip()
                    for tag in tags
                    if tag.strip()
                ],
            )

        # 2. JSON-LD JobPosting — заполняет только то, чего нет выше.
        json_ld_match = re.search(
            r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
            html,
            re.IGNORECASE | re.DOTALL,
        )
        if json_ld_match:
            try:
                data = json.loads(json_ld_match.group(1))
                if isinstance(data, dict) and data.get("@type") == "JobPosting":
                    for key, value in self._extract_from_json_ld(data).items():
                        fill(key, value)
            except (json.JSONDecodeError, KeyError):
                pass

        # 3. Мета-теги.
        meta_patterns = {
            "title": r'<meta[^>]*property=["\']og:title["\'][^>]*content=["\']([^"\']*)["\']',
            "company_name": r'<meta[^>]*property=["\']og:site_name["\'][^>]*content=["\']([^"\']*)["\']',
            "description_raw": r'<meta[^>]*name=["\']description["\'][^>]*content=["\']([^"\']*)["\']',
        }
        for key, pattern in meta_patterns.items():
            match = re.search(pattern, html, re.IGNORECASE)
            if match:
                value = html_lib.unescape(match.group(1).strip())
                fill(key, value or None)
        if fields.get("description_raw") and not fields.get("description_html"):
            fields["description_html"] = fields["description_raw"]

        # 4. <title>: «Backend Developer @ Globes | Remotive» → «Backend Developer».
        title_tag = re.search(r"<title[^>]*>([^<]+?)</title>", html, re.IGNORECASE)
        if title_tag:
            content = html_lib.unescape(title_tag.group(1)).strip()
            head = re.split(r"\s+[@|–—]\s+|\s+[-–—]\s+", content, maxsplit=1)[0].strip()
            fill("title", head or None)

        # 5. Последний шанс — разбор ссылок выдачи (могут попасться карточки).
        if not fields:
            jobs = parse_html_jobs(html)
            if jobs:
                job = jobs[0]
                fill("title", job.get("title"))
                fill("company_name", job.get("company_name"))
                fill("location", job.get("location"))
                fill("description_raw", job.get("description_raw"))
                fill("tags", job.get("tags") or None)

        return fields

    # --- вспомогательное -----------------------------------------------------

    @staticmethod
    def _resolve_external_id(external_id_or_url: str) -> str | None:
        """Внешний id из external_id или прямой ссылки карточки."""
        value = (external_id_or_url or "").strip()
        if not value:
            return None
        if value.startswith(("http://", "https://")):
            # Extract ID from URL like https://remotive.com/remote-jobs/category/slug-123456
            match = re.search(r'/(\d+)(?:[/?#]|$)', value)
            if match:
                return match.group(1)
            # Alternative pattern: slug-id
            match = re.search(r'-(\d+)(?:[/?#]|$)', value)
            if match:
                return match.group(1)
        return value[:32]  # Limit length for safety

    @staticmethod
    def _parse_api_response(response_text: str) -> list[dict]:
        """Разбор ответа API: ``{"jobs": [...]}`` либо плоский JSON-массив.

        Первые элементы плоского массива — ``00-warning``/``0-legal-notice``:
        они (и любые не-вакансии без ``id``) отбрасываются.
        """
        data = json.loads(response_text)
        if isinstance(data, dict):
            jobs = data.get("jobs")
            if not isinstance(jobs, list):
                raise ValueError("API response has no 'jobs' list")
        elif isinstance(data, list):
            jobs = data
        else:
            raise ValueError("API response is not a JSON object or array")
        return [
            job for job in jobs if isinstance(job, dict) and job.get("id") not in (None, "")
        ]

    @staticmethod
    def _extract_from_json_ld(data: dict) -> dict:
        """Extract job fields from JSON-LD JobPosting."""
        fields = {}
        
        # Title
        if "title" in data:
            fields["title"] = str(data["title"]).strip()
        
        # Company
        if "hiringOrganization" in data:
            org = data["hiringOrganization"]
            if isinstance(org, dict) and "name" in org:
                fields["company_name"] = str(org["name"]).strip()
        
        # Description
        if "description" in data:
            desc = data["description"]
            if isinstance(desc, str):
                fields["description_raw"] = desc
                fields["description_html"] = desc
        
        # Date posted
        if "datePosted" in data:
            fields["published_at"] = str(data["datePosted"])
        
        # Employment type
        if "employmentType" in data:
            emp_type = data["employmentType"]
            if isinstance(emp_type, str):
                fields["employment_form"] = emp_type
            elif isinstance(emp_type, list) and emp_type:
                fields["employment_form"] = emp_type[0]
        
        # Work schedule
        if "workHours" in data:
            fields["schedule"] = str(data["workHours"])
        
        return fields