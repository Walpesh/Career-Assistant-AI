"""Адаптер источника RemoteOK (docs/04_PARSING_RULES.md §10).

Площадка удалённой работы remoteok.com. Два пути получения данных:

- **Основной** — публичный API ``https://remoteok.com/api`` (без ключа):
  JSON-массив вакансий; ПЕРВЫЙ элемент — legal-уведомление Remote OK,
  он пропускается. Запрос идёт через общую ``AntiBanSession`` оркестратора.
- **Fallback** — при отказе/троттлинге API (429, не-JSON, сетевая ошибка)
  лёгкий HTML-парсинг главной страницы ``remoteok.com`` через ``curl_cffi``
  (docs/04 §2 п.1): из разметки извлекаются ссылки ``/l/<id>`` и
  ``/remote-jobs/<slug>-<id>`` вместе с data-атрибутами карточки.

Нормализация (``normalize``) приводит сырые данные к канонической схеме
docs/04 §7 (ключи ``source``, ``external_id``, ``url``, ``title``,
``company_name``, ``salary_from/to/currency``, ``experience``,
``employment_form``, ``work_format``, ``schedule``, ``area``,
``published_at``, ``description_raw``, ``description_html``). Дополнительно
отдаются поля задачи: ``tags``, ``remote=True`` и алиасы
``company``/``currency``/``description``. Все вакансии RemoteOK —
удалённые (``remote=True``, ``work_format="remote"``).

Anti-ban (прогрев, паузы 4–8 с, ротация прокси) остаётся в Proxy &
Anti-Ban Module: API и карточки запрашиваются через готовую
``AntiBanSession``; curl_cffi-fallback — исключение (одиночный лёгкий
GET, см. ``_fetch_html``).
"""

from __future__ import annotations

import asyncio
import html as html_lib
import json
import logging
import re
from urllib.parse import urlsplit

from app.modules.anti_ban import AntiBanSession
from app.modules.parsing.fetcher import build_page_fetcher
from app.modules.parsing.sources.base import BaseSourceAdapter

__all__ = [
    "API_URL",
    "HOME_URL",
    "LISTING_URL",
    "RemoteOKAPIError",
    "RemoteOKAdapter",
    "extract_card_fields",
    "parse_html_jobs",
]

logger = logging.getLogger(__name__)

#: Публичный API RemoteOK (без ключа); первый элемент массива — legal.
API_URL = "https://remoteok.com/api"
#: Главная страница — цель HTML-fallback при отказе/троттлинге API.
HOME_URL = "https://remoteok.com"
#: Лента вакансий (хуки оркестратора и валидация ссылок выдачи).
LISTING_URL = "https://remoteok.com/remote-jobs"
#: Прямая ссылка карточки по external_id.
JOB_URL_TEMPLATE = "https://remoteok.com/l/{external_id}"

#: Разрешённые хосты ссылок выдачи (docs/04 §4.2 — белый список доменов).
_ALLOWED_HOSTS = ("remoteok.com", "www.remoteok.com")
#: Статусы «вакансия удалена» (docs/04 §5 → get_vacancy вернёт None).
_NOT_FOUND_STATUSES = (404, 410)

#: Окно контекста вокруг якоря карточки для data-атрибутов (best-effort).
_CONTEXT_BEFORE = 1500
_CONTEXT_AFTER = 1500

#: Якорь карточки: <a ... href="/l/<id>" ...>заголовок</a> или
#: <a ... href="/remote-jobs/<slug>-<id>" ...>заголовок</a>.
_ANCHOR_RE = re.compile(
    r"<a\b(?P<pre>[^>]*?)\s+href=[\"'](?:(?:https?:)?//(?:www\.)?remoteok\.com)?/"
    r"(?:l|remote-jobs)/(?P<slug>[^\"'#?]+)[\"'](?P<post>[^>]*)>(?P<title>.*?)</a>",
    re.IGNORECASE | re.DOTALL,
)
#: Ссылка пагинации «следующая страница» (признак has_next в parse_listing).
_NEXT_LINK_RE = re.compile(
    r"<a\b[^>]*\srel=[\"'][^\"']*\bnext\b[^\"']*[\"'][^>]*>", re.IGNORECASE
)
#: Числовой идентификатор в конце слага: «...-111111» → «111111».
_TRAILING_ID_RE = re.compile(r"(\d+)$")
#: Числа вида «$80,000» / «100k» из data-salary.
_SALARY_NUMBER_RE = re.compile(r"(\d+(?:,\d{3})*(?:\.\d+)?)\s*(k)?", re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")
#: Блочные теги — заменяются пробелом, чтобы слова не склеивались.
_BLOCK_TAG_RE = re.compile(
    r"</?\s*(?:p|div|br|li|ul|ol|h[1-6]|tr|td|section|article|header|footer)\b[^>]*>",
    re.IGNORECASE,
)
_SPACE_RE = re.compile(r"\s+")
#: Суффикс «| Remote OK» в <title>/og:title карточки.
_TITLE_SUFFIX_RE = re.compile(r"\s*[|–—-]\s*Remote\s*OK\s*$", re.IGNORECASE)
_PATH_ID_RE = re.compile(r"/(?:l|remote-jobs)/([^/?#]+)")


class RemoteOKAPIError(RuntimeError):
    """API RemoteOK недоступен или ответ не парсится — включается HTML-fallback."""


# --- общие хелперы нормализации ---------------------------------------------


def _strip_html(value: object) -> str | None:
    """HTML → плоский текст (теги и сущности убираются, пробелы схлопываются)."""
    if value is None:
        return None
    text = _BLOCK_TAG_RE.sub(" ", str(value))  # блочные теги → пробел
    text = _TAG_RE.sub("", text)  # строчные теги (<b>, <i>) → без пробела
    text = html_lib.unescape(text)
    text = _SPACE_RE.sub(" ", text).strip()
    return text or None


def _as_text(value: object, *, limit: int | None = None) -> str | None:
    """Строковое значение: None/пусто → None, длинное — обрезается."""
    if value is None:
        return None
    result = str(value).strip()
    if not result:
        return None
    return result[:limit] if limit else result


def _as_int(value: object) -> int | None:
    """Целое зарплаты: нечисловое → None; 0 → None (0 = «нет данных» RemoteOK)."""
    if value is None or isinstance(value, bool):
        return None
    try:
        result = int(float(str(value).strip()))
    except (TypeError, ValueError):
        return None
    return result if result > 0 else None


def _id_from_slug(slug: str) -> str | None:
    """Внешний id из слага: «...-111111» → «111111», «111111» → «111111»."""
    match = _TRAILING_ID_RE.search((slug or "").strip())
    return match.group(1) if match else None


def _job_url(external_id: str) -> str:
    """Прямая ссылка карточки RemoteOK по внешнему id."""
    return JOB_URL_TEMPLATE.format(external_id=external_id)


def _normalize_url(url: object) -> str | None:
    """URL карточки; хост remoteOK.com приводится к каноническому регистру."""
    text = _as_text(url)
    if not text:
        return None
    return text.replace("remoteOK.com", "remoteok.com")


def _external_id_of(job: dict) -> str | None:
    """Внешний id вакансии: прямой id → из ссылки карточки."""
    for candidate in (job.get("external_id"), job.get("id")):
        text = _as_text(candidate)
        if text:
            return text[:32]  # vacancies.external_id — VARCHAR(32) (docs/02 §3.3)
    match = _PATH_ID_RE.search(_as_text(job.get("url")) or "")
    if match:
        external_id = _id_from_slug(match.group(1))
        if external_id:
            return external_id[:32]
    return None


def _split_tags(value: str | None) -> list[str]:
    """«python,go» / «python go» → ["python", "go"]."""
    if not value:
        return []
    parts = value.split(",") if "," in value else value.split()
    return [part.strip() for part in parts if part and part.strip()]


def _tags_of(job: dict) -> list[str]:
    """Список тегов вакансии (API отдаёт список, HTML — строку)."""
    raw = job.get("tags")
    if raw is None:
        return []
    if isinstance(raw, str):
        return _split_tags(raw)
    if isinstance(raw, (list, tuple, set)):
        return [text for text in (str(item).strip() for item in raw) if text]
    return []


def _as_words(value: object) -> list[str]:
    """Нормализовать ключевые слова/теги фильтра: строка через запятую или список."""
    if value is None:
        return []
    if isinstance(value, str):
        parts = value.split(",")
    elif isinstance(value, (list, tuple, set)):
        parts = [str(item) for item in value]
    else:
        return []
    return [word.strip() for word in parts if word and word.strip()]

# --- хелперы разбора HTML ----------------------------------------------------


def _matches_filters(job: dict, keywords: list[str], tags: list[str]) -> bool:
    """Проверка сырой карточки по ключевым словам и тегам.

    Keywords: КАЖДОЕ слово должно встретиться в названии/компании/локации/
    тегах/описании (AND, регистронезависимо). Tags: достаточно ЛЮБОГО
    совпадения с тегами вакансии (OR).
    """
    if keywords:
        haystack = " ".join(
            part
            for part in (
                str(job.get("position") or job.get("title") or ""),
                str(job.get("company") or job.get("company_name") or ""),
                str(job.get("location") or job.get("area") or ""),
                " ".join(_tags_of(job)),
                _strip_html(job.get("description") or job.get("description_raw")) or "",
            )
            if part
        ).casefold()
        if not all(word.casefold() in haystack for word in keywords):
            return False
    if tags:
        job_tags = {tag.casefold() for tag in _tags_of(job)}
        if not any(tag.casefold() in job_tags for tag in tags):
            return False
    return True


def _attr(fragment: str, name: str) -> str | None:
    """Значение атрибута (data-*, class-подобных) в фрагменте разметки."""
    match = re.search(
        rf"\b{re.escape(name)}\s*=\s*[\"'](?P<value>[^\"']*)[\"']", fragment, re.IGNORECASE
    )
    return _as_text(match.group("value")) if match else None


def _class_inner(fragment: str, class_name: str) -> str | None:
    """Внутренний HTML первого элемента с точным токеном class.

    Открывающий тег ищется отдельно от закрывающего: иначе вложенный
    элемент (div внутри div) «съедает» искомый блок при поиске пар.
    """
    opening_re = re.compile(
        r"<(?P<tag>\w+)\b[^>]*?\bclass=[\"'](?P<cls>[^\"']*)[\"'][^>]*>",
        re.IGNORECASE,
    )
    for match in opening_re.finditer(fragment):
        if class_name not in match.group("cls").split():
            continue
        closing = re.search(
            rf"</{re.escape(match.group('tag'))}\s*>",
            fragment[match.end() :],
            re.IGNORECASE,
        )
        if closing is None:
            continue
        return fragment[match.end() : match.end() + closing.start()]
    return None


def _meta_content(fragment: str, *, prop: str | None = None, name: str | None = None) -> str | None:
    """content= нужного <meta> (og:title / description)."""
    for tag in re.findall(r"<meta\b[^>]*>", fragment, re.IGNORECASE):
        if prop is not None and (_attr(tag, "property") or "").casefold() == prop.casefold():
            return _as_text(_attr(tag, "content"))
        if name is not None and (_attr(tag, "name") or "").casefold() == name.casefold():
            return _as_text(_attr(tag, "content"))
    return None


def _itemprop_value(fragment: str, prop: str) -> str | None:
    """Значение элемента с itemprop: <meta content> либо внутренний текст."""
    meta = re.search(rf"<meta\b[^>]*itemprop=[\"']{re.escape(prop)}[\"'][^>]*>", fragment, re.IGNORECASE)
    if meta:
        return _as_text(_attr(meta.group(0), "content"))
    match = re.search(
        rf"<(\w+)\b[^>]*itemprop=[\"']{re.escape(prop)}[\"'][^>]*>(?P<inner>.*?)</\1>",
        fragment,
        re.IGNORECASE | re.DOTALL,
    )
    return _strip_html(match.group("inner")) if match else None


def _clean_title(title: str | None) -> str | None:
    """Убрать служебный суффикс «| Remote OK» из заголовка карточки."""
    text = _as_text(title)
    if not text:
        return None
    return _as_text(_TITLE_SUFFIX_RE.sub("", text))


def _slug_title(slug: str) -> str | None:
    """Заголовок из слага: «backend-dev-acme-111111» → «backend dev acme»."""
    base = _TRAILING_ID_RE.sub("", (slug or "").strip()).rstrip("-")
    text = _SPACE_RE.sub(" ", base.replace("-", " ")).strip()
    return text or None


def _parse_salary_range(value: str | None) -> tuple[int | None, int | None]:
    """«$80,000 - $110,000» / «100k» → (80000, 110000) / (100000, None)."""
    if not value:
        return (None, None)
    amounts: list[int] = []
    for number, k_suffix in _SALARY_NUMBER_RE.findall(value):
        amount = float(number.replace(",", ""))
        if k_suffix:
            amount *= 1000
        amounts.append(int(amount))
    if not amounts:
        return (None, None)
    if len(amounts) == 1:
        return (amounts[0], None)
    return (amounts[0], amounts[1])


def _fetch_html(
    url: str, *, timeout: float = 20.0, impersonate: str = "chrome"
) -> str:
    """Лёгкий GET через curl_cffi (docs/04 §2 п.1) — HTML-fallback RemoteOK.

    Вынесен в модульную функцию, чтобы тесты подменяли его без сети.
    """
    from curl_cffi import requests as curl_requests

    response = curl_requests.get(url, impersonate=impersonate, timeout=timeout)
    status = int(getattr(response, "status_code", 200) or 200)
    if status != 200:
        raise RemoteOKAPIError(f"HTTP {status} от {url}")
    return str(response.text or "")


# --- разбор HTML-выдачи и карточки -------------------------------------------


def parse_html_jobs(html_text: str) -> list[dict]:
    """Разобрать HTML-выдачу remoteok.com в сырые карточки (best-effort).

    Стратегия: найти все якоря ``/l/<id>`` и ``/remote-jobs/<slug>-<id>``,
    id взять из слага, а company/location/tags/salary — из data-атрибутов
    контекста карточки вокруг якоря. Порядок выдачи сохраняется, дубли
    по id отбрасываются.
    """
    jobs: list[dict] = []
    seen: set[str] = set()
    previous_end = 0
    for match in _ANCHOR_RE.finditer(html_text):
        external_id = _id_from_slug(match.group("slug"))
        # Контекст не «перепрыгивает» предыдущий якорь — атрибуты соседней
        # карточки не попадают в текущую.
        start = max(previous_end, match.start() - _CONTEXT_BEFORE)
        end = min(len(html_text), match.end() + _CONTEXT_AFTER)
        previous_end = match.end()
        if not external_id or external_id in seen:
            continue
        seen.add(external_id)
        context = html_text[start:end]
        position = (
            _strip_html(match.group("title"))
            or _attr(context, "data-position")
            or _slug_title(match.group("slug"))
        )
        salary_from, salary_to = _parse_salary_range(_attr(context, "data-salary"))
        jobs.append(
            {
                "id": external_id,
                "position": position,
                "company": _attr(context, "data-company") or _class_inner_text(context, "company"),
                "location": _attr(context, "data-location") or _class_inner_text(context, "location"),
                "tags": _split_tags(_attr(context, "data-tags")),
                "salary_min": salary_from,
                "salary_max": salary_to,
                "url": _job_url(external_id),
            }
        )
    return jobs


def _class_inner_text(fragment: str, class_name: str) -> str | None:
    """Плоский текст элемента с данным class (company, location)."""
    return _strip_html(_class_inner(fragment, class_name))


def extract_card_fields(html_text: str) -> dict:
    """Поля карточки вакансии RemoteOK из HTML (docs/04 §7, best-effort).

    Источники: og:title/<h1>/<title>, data-атрибуты и itemprop карточки,
    <div class="description">, meta description, data-tags/data-salary.
    """
    title = _meta_content(html_text, prop="og:title")
    if not title:
        heading = re.search(r"<h1\b[^>]*>(?P<inner>.*?)</h1>", html_text, re.IGNORECASE | re.DOTALL)
        title = _strip_html(heading.group("inner")) if heading else None
    if not title:
        page_title = re.search(
            r"<title\b[^>]*>(?P<inner>.*?)</title>", html_text, re.IGNORECASE | re.DOTALL
        )
        title = _strip_html(page_title.group("inner")) if page_title else None

    description_html = _class_inner(html_text, "description")
    description_raw = _strip_html(description_html) or _meta_content(
        html_text, name="description"
    )
    location = (
        _attr(html_text, "data-location")
        or _class_inner_text(html_text, "location")
        or _itemprop_value(html_text, "jobLocation")
    )
    salary_from, salary_to = _parse_salary_range(_attr(html_text, "data-salary"))
    return {
        "title": _clean_title(title),
        "company": (
            _attr(html_text, "data-company")
            or _class_inner_text(html_text, "company")
            or _itemprop_value(html_text, "hiringOrganization")
        ),
        "location": location,
        "area": location,
        "description_html": description_html,
        "description_raw": description_raw,
        "tags": _split_tags(_attr(html_text, "data-tags")),
        "salary_from": salary_from,
        "salary_to": salary_to,
    }


# --- адаптер источника -------------------------------------------------------


class RemoteOKAdapter(BaseSourceAdapter):
    """Источник RemoteOK: публичный API + HTML-fallback через curl_cffi."""

    #: Имя источника в SourceRegistry и в vacancies.source адаптера.
    source_name = "remoteok"

    def __init__(self, session: AntiBanSession | None = None) -> None:
        # Сессия та же, что у оркестратора: единые прогрев/паузы/прокси и
        # общая статистика трафика (docs/04 §3). Свою создаёт только при
        # прямом использовании адаптера вне оркестратора.
        self.http = session or AntiBanSession(fetcher=build_page_fetcher())

    # --- контракт BaseSourceAdapter (docs/04 §10.1) --------------------------

    async def search(self, filters: dict) -> list[dict]:
        """Собрать карточки выдачи RemoteOK по фильтрам.

        Args:
            filters: ``keywords`` (ключевые слова, AND — по названию, компании,
                локации, тегам и описанию), ``tags``/``tag`` (теги, OR),
                ``max_results``/``limit`` (ограничение числа карточек).
                RemoteOK-API не принимает поисковый запрос в URL, поэтому
                фильтрация выполняется после получения данных.

        Returns:
            list[dict]: сырые карточки с обязательными полями ``source``,
            ``external_id`` и ``url``; порядок — как на выдаче, без дублей.
        """
        options = filters or {}
        keywords = _as_words(options.get("keywords"))
        tag_filter = options.get("tags")
        if tag_filter is None:
            tag_filter = options.get("tag")
        tags = _as_words(tag_filter)
        limit = _as_int(options.get("max_results") or options.get("limit"))

        jobs = await self._fetch_jobs()
        results: list[dict] = []
        seen: set[str] = set()
        for job in jobs:
            if not isinstance(job, dict):
                continue
            external_id = _external_id_of(job)
            if not external_id or external_id in seen:
                continue
            if not _matches_filters(job, keywords, tags):
                continue
            seen.add(external_id)
            card = dict(job)
            card["source"] = self.source_name
            card["external_id"] = external_id
            card["url"] = _normalize_url(card.get("url")) or _job_url(external_id)
            results.append(card)
            if limit is not None and len(results) >= limit:
                break
        return results

    async def get_vacancy(self, external_id_or_url: str) -> dict | None:
        """Карточка одной вакансии RemoteOK по external_id или прямой ссылке.

        Основной путь — API; при отказе API карточка запрашивается напрямую
        (HTML-fallback). None — вакансия не найдена/удалена (docs/04 §5).
        """
        external_id = self._resolve_external_id(external_id_or_url)
        if not external_id:
            return None
        try:
            jobs = await self._fetch_api_jobs()
        except Exception as exc:  # noqa: BLE001 — API упал, берём карточку по ссылке
            logger.warning("RemoteOK API недоступен (%s) — карточка по ссылке", exc)
            return await self._fetch_card_via_html(external_id)
        for job in jobs:
            if _external_id_of(job) == external_id:
                return self.normalize(job)
        return None

    async def _fetch_card_via_html(self, external_id: str) -> dict | None:
        """Карточка через прямую ссылку: 404/410 → None, иначе разбор HTML."""
        url = _job_url(external_id)
        response = await self.http.fetch(url)
        if response.status_code in _NOT_FOUND_STATUSES:
            return None
        fields = extract_card_fields(response.text)
        if not fields.get("title"):
            # Страница получена, но карточка не разобрана (капча/вёрстка).
            raise RemoteOKAPIError(f"Не удалось разобрать карточку {url}")
        return self.normalize({"source": self.source_name, "external_id": external_id, "url": url, **fields})

    async def _fetch_jobs(self) -> list[dict]:
        """Сырые карточки: API → HTML-fallback при отказе/троттлинге."""
        try:
            return await self._fetch_api_jobs()
        except Exception as exc:  # noqa: BLE001 — любой сбой API включает fallback
            logger.warning(
                "RemoteOK API недоступен (%s) — HTML-fallback через curl_cffi", exc
            )
        return await self._fetch_html_jobs()

    async def _fetch_api_jobs(self) -> list[dict]:
        """Публичный API https://remoteok.com/api (первый элемент — legal)."""
        response = await self.http.fetch(API_URL)
        if response.status_code != 200:
            raise RemoteOKAPIError(f"HTTP {response.status_code} от {API_URL}")
        try:
            data = json.loads(response.text)
        except (TypeError, ValueError) as exc:
            raise RemoteOKAPIError(f"JSON API не разобран: {exc}") from exc
        if not isinstance(data, list):
            raise RemoteOKAPIError("Ожидался JSON-массив вакансий RemoteOK")
        # Первый элемент массива — legal-уведомление Remote OK, его пропускаем.
        return [
            item
            for item in data
            if isinstance(item, dict) and "legal" not in item and item.get("id") is not None
        ]

    async def _fetch_html_jobs(self) -> list[dict]:
        """Fallback: лёгкий HTML-парсинг главной remoteok.com (curl_cffi)."""
        html_text = await asyncio.to_thread(_fetch_html, HOME_URL)
        jobs = parse_html_jobs(html_text)
        if not jobs:
            logger.warning("RemoteOK HTML-fallback: карточки в выдаче не найдены")
        return jobs

    def normalize(self, raw: dict) -> dict:
        """Сырые данные RemoteOK → каноническая схема вакансии (docs/04 §7).

        Плюс поля задачи: ``tags``, ``remote=True`` и алиасы ``company`` /
        ``currency`` / ``description``. Зарплаты RemoteOK публикуются в USD;
        0/отсутствие salary_min/max трактуется как «нет данных».
        """
        external_id = _external_id_of(raw)
        url = _normalize_url(raw.get("url"))
        if not url and external_id:
            url = _job_url(external_id)

        description_html = raw.get("description_html") or raw.get("description")
        if description_html is not None:
            description_html = str(description_html)
        description_raw = _as_text(raw.get("description_raw")) or _strip_html(
            description_html
        )

        salary_from = _as_int(raw.get("salary_from")) or _as_int(raw.get("salary_min"))
        salary_to = _as_int(raw.get("salary_to")) or _as_int(raw.get("salary_max"))
        currency = _as_text(
            raw.get("salary_currency") or raw.get("currency"), limit=8
        )
        if not currency and (salary_from or salary_to):
            currency = "USD"  # зарплаты RemoteOK всегда в USD

        title = _clean_title(raw.get("title") or raw.get("position"))
        company = _as_text(raw.get("company_name") or raw.get("company"), limit=512)
        area = _as_text(raw.get("area") or raw.get("location"), limit=255)
        return {
            # каноническая схема docs/04 §7
            "source": self.source_name,
            "external_id": external_id,
            "url": url,
            "title": _as_text(title, limit=512),
            "company_name": company,
            "salary_from": salary_from,
            "salary_to": salary_to,
            "salary_currency": currency,
            "experience": _as_text(raw.get("experience"), limit=64),
            "employment_form": _as_text(raw.get("employment_form"), limit=64),
            "work_format": _as_text(raw.get("work_format")) or "remote",
            "schedule": _as_text(raw.get("schedule"), limit=128),
            "area": area,
            "published_at": raw.get("published_at") or raw.get("date"),
            "description_raw": description_raw,
            "description_html": description_html,
            # специфика RemoteOK/задачи
            "tags": _tags_of(raw),
            "remote": True,
            "company": company,
            "currency": currency,
            "description": description_raw,
        }

    # --- хуки ParsingOrchestrator (docs/04 §10.2) ----------------------------

    def build_search_url(
        self,
        *,
        keywords: list[str] | None = None,
        tags: list[str] | None = None,
        page: int = 0,
        **_kwargs: object,
    ) -> str:
        """Ссылка выдачи автопоиска (docs/04 §4.1).

        RemoteOK-API не принимает ключевые слова в URL: keywords/tags
        фильтруются в ``search``, URL — лента вакансий с пагинацией.
        """
        return self.page_url(LISTING_URL, int(page or 0))

    def validate_search_url(self, search_url: str) -> str:
        """Принимаются только ссылки выдачи remoteok.com (docs/04 §4.2)."""
        candidate = str(search_url).strip()
        parts = urlsplit(candidate)
        host = (parts.hostname or "").lower()
        if parts.scheme not in ("http", "https") or host not in _ALLOWED_HOSTS:
            raise ValueError(f"Ссылка выдачи RemoteOK должна быть на remoteok.com: {candidate!r}")
        return candidate

    def page_url(self, base_url: str, page: int) -> str:
        """Страница выдачи: 0-based индекс → параметр ``page`` с единицы."""
        index = max(0, int(page or 0))
        if index == 0:
            return base_url
        joiner = "&" if "?" in base_url else "?"
        return f"{base_url}{joiner}page={index + 1}"

    def parse_listing(self, html: str) -> tuple[list[str], bool]:
        """Карточки выдачи + признак пагинации (HTML-fallback разбор)."""
        jobs = parse_html_jobs(html)
        return [str(job["id"]) for job in jobs], bool(_NEXT_LINK_RE.search(html))

    def build_vacancy_url(self, base_url: str, external_id: str) -> str:
        """Прямая ссылка карточки: https://remoteok.com/l/<id>."""
        return _job_url(str(external_id).strip())

    def extract_fields(self, html: str) -> dict:
        """Поля карточки RemoteOK из HTML (docs/04 §7)."""
        return extract_card_fields(html)

    # --- вспомогательное -----------------------------------------------------

    @staticmethod
    def _resolve_external_id(external_id_or_url: str) -> str | None:
        """Внешний id из external_id или прямой ссылки карточки."""
        value = (external_id_or_url or "").strip()
        if not value:
            return None
        if value.startswith(("http://", "https://")):
            match = _PATH_ID_RE.search(value)
            return _id_from_slug(match.group(1)) if match else None
        return value[:32]




