"""Адаптер источника We Work Remotely (docs/04_PARSING_RULES.md §10).

Площадка удалённой работы weworkremotely.com. Два пути получения данных:

- **Основной** — официальные RSS-ленты: общая
  ``https://weworkremotely.com/remote-jobs.rss`` и ленты категорий
  (``/categories/remote-*-jobs.rss``, см. ``CATEGORY_PAGES``). Запрос идёт
  через общую ``AntiBanSession`` оркестратора. Элемент ленты содержит
  заголовок вида «Company: Role», ``<region>``, ``<country>``, ``<state>``,
  ``<skills>``, ``<category>``, ``<type>``, ``<pubDate>`` и HTML-описание.
- **Fallback** — при отказе/непригодности RSS (4xx/5xx, не XML, пустая
  лента) HTML-разбор страницы категории: сначала лёгкий GET через
  ``curl_cffi`` (docs/04 §2 п.1, см. ``_fetch_html``), при его сбое —
  надёжный путь Playwright через ``AntiBanSession``.

Заголовок «Company: Role» разбирается ``split_company_title``: компания —
до первого «:», роль — остаток. Регион — из ``<region>`` (фолбэк —
``<country>``/``<state>``); на HTML-странице регион восстанавливается из
флагов стран карточки (best-effort). Скиллы — из ``<skills>`` либо
выводятся из заголовка и описания (``_infer_skills``).

Внешний идентификатор: слаг ссылки вакансии превращается в детерминированный
хеш ``sha256[:24]`` — слаги нередко длиннее 32 символов, а
``vacancies.external_id``/``hh_vacancy_id`` — VARCHAR(32) (docs/02 §3.3).
Хеш считается и от ``<guid>`` ленты, и от ``href`` страницы, поэтому оба
пути дают одинаковый id; прямая ссылка кэшируется в ``_url_by_id``, чтобы
``build_vacancy_url`` мог собрать карточку из id (docs/04 §10.2).

Нормализация (``normalize``) приводит сырые данные к канонической схеме
docs/04 §7 (ключи ``source``, ``external_id``, ``url``, ``title``,
``company_name``, ``salary_from/to/currency``, ``experience``,
``employment_form``, ``work_format``, ``schedule``, ``area``,
``published_at``, ``description_raw``, ``description_html``) плюс поля
задачи: ``category``, ``region``, ``skills``, ``tags``, ``remote=True``.
Зарплата в ленте WWR не публикуется — ``salary_*`` всегда None.

Anti-ban (прогрев, паузы 4–8 с, ротация прокси) остаётся в Proxy &
Anti-Ban Module: лента и карточки запрашиваются через готовую
``AntiBanSession``; curl_cffi-fallback — исключение (одиночный лёгкий
GET, см. ``_fetch_html``).
"""

from __future__ import annotations

import asyncio
import hashlib
import html as html_lib
import logging
import re
import xml.etree.ElementTree as ET
from urllib.parse import urlsplit

from app.modules.anti_ban import AntiBanSession
from app.modules.parsing.fetcher import build_page_fetcher
from app.modules.parsing.sources.base import BaseSourceAdapter

__all__ = [
    "CATEGORY_PAGES",
    "HOME_URL",
    "LISTING_URL",
    "RSS_URL",
    "WeWorkRemotelyAdapter",
    "WeWorkRemotelyError",
    "category_display",
    "category_for_listing",
    "extract_card_fields",
    "external_id_for_url",
    "feed_url_for",
    "listing_url_for",
    "parse_html_jobs",
    "parse_rss",
    "split_company_title",
]

logger = logging.getLogger(__name__)

#: Главная страница — общая выдача и цель HTML-fallback общей ленты.
HOME_URL = "https://weworkremotely.com"
#: Официальная общая RSS-лента (основной путь).
RSS_URL = "https://weworkremotely.com/remote-jobs.rss"
#: Общая выдача (HTML-fallback для ``RSS_URL``).
LISTING_URL = "https://weworkremotely.com/"

#: Ключ категории → (путь страницы выдачи, название категории в ``<category>``).
#: Пути страниц сверены с навигацией weworkremotely.com; лента страницы —
#: тот же путь с суффиксом ``.rss`` (см. ``feed_url_for``).
CATEGORY_PAGES: dict[str, tuple[str, str]] = {
    "programming": ("/categories/remote-programming-jobs", "Programming"),
    "full-stack-programming": (
        "/categories/remote-full-stack-programming-jobs",
        "Full-Stack Programming",
    ),
    "front-end-programming": (
        "/categories/remote-front-end-programming-jobs",
        "Front-End Programming",
    ),
    "back-end-programming": (
        "/categories/remote-back-end-programming-jobs",
        "Back-End Programming",
    ),
    "design": ("/categories/remote-design-jobs", "Design"),
    "product-design": ("/categories/remote-product-design-jobs", "Product Design"),
    "ux-ui-design": ("/categories/remote-ux-ui-design-jobs", "UX/UI Design"),
    "devops-sysadmin": ("/categories/remote-devops-sysadmin-jobs", "Devops and Sysadmin"),
    "management-finance": (
        "/categories/remote-management-and-finance-jobs",
        "Management and Finance",
    ),
    "product": ("/categories/remote-product-jobs", "Product"),
    "customer-support": ("/categories/remote-customer-support-jobs", "Customer Support"),
    "sales-marketing": (
        "/categories/remote-sales-and-marketing-jobs",
        "Sales and Marketing",
    ),
}

#: Разрешённые хосты ссылок выдачи (docs/04 §4.2 — белый список доменов).
_ALLOWED_HOSTS = ("weworkremotely.com", "www.weworkremotely.com")
#: Статусы «вакансия удалена» (docs/04 §5 → get_vacancy вернёт None).
_NOT_FOUND_STATUSES = (404, 410)

#: Значения ``<type>`` ленты / метки выдачи → формы занятости.
_EMPLOYMENT_TYPES = frozenset(
    {
        "full-time",
        "full time",
        "part-time",
        "part time",
        "contract",
        "internship",
        "temporary",
    }
)

#: Якорь карточки выдачи: <a href="/remote-jobs/<slug>">...<span class="...title__text">...</span>...</a>.
_LISTING_ANCHOR_RE = re.compile(
    r'<a\b[^>]*\bhref="(?:(?:https?:)?//(?:www\.)?weworkremotely\.com)?/remote-jobs/'
    r'(?P<slug>[^"#?]+)"[^>]*>(?P<body>.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)
_TITLE_TEXT_RE = re.compile(
    r'class="new-listing__header__title__text"[^>]*>(?P<title>.*?)</span>', re.DOTALL
)
_COMPANY_RE = re.compile(r'class="new-listing__company-name"[^>]*>(?P<company>.*?)</p>', re.DOTALL)
_HQ_INNER_RE = re.compile(
    r'class="new-listing__company-headquarters"[^>]*>(?P<hq>.*?)</p>', re.DOTALL
)
_HQ_LABEL_RE = re.compile(
    r'<span class="new-listing__company-headquarters__label">.*?</span>', re.DOTALL
)
_CATEGORY_ITEM_RE = re.compile(
    r'class="new-listing__categories__category[^"]*"[^>]*>(?P<text>.*?)</p>', re.DOTALL
)
#: Эмодзи-флаг (региональный индикатор) в метках стран карточки.
_FLAG_RE = re.compile(r"[\U0001F1E6-\U0001F1FF]{2}")
#: Ссылка пагинации «следующая страница» (признак has_next в parse_listing).
_NEXT_LINK_RE = re.compile(r'<a\b[^>]*\brel=["\'][^"\']*\bnext\b[^"\']*["\'][^>]*>', re.IGNORECASE)

#: Заголовок карточки: <h1 class="lis-container__header__hero__company-info__title">Роль</h1>.
_CARD_TITLE_RE = re.compile(
    r'<h1 class="lis-container__header__hero__company-info__title"[^>]*>(?P<title>.*?)</h1>',
    re.DOTALL,
)
_CARD_PAGE_TITLE_RE = re.compile(r"<title[^>]*>(?P<title>.*?)</title>", re.DOTALL)
#: Имя компании из hero-описания: <div ...__description><div><a ...>Компания</a>.
_CARD_COMPANY_RE = re.compile(
    r'class="lis-container__header__hero__company-info__description"[^>]*>\s*<div>\s*'
    r"<a[^>]*>(?P<company>.*?)</a>",
    re.DOTALL,
)
_CARD_COMPANY_SLUG_RE = re.compile(r'href="/company/(?P<slug>[^"/?#]+)"')
#: Описание карточки: <div class="lis-container__job__content__description">...</div>.
_CARD_DESC_RE = re.compile(
    r'class="lis-container__job__content__description"[^>]*>(?P<desc>.*?)</div>', re.DOTALL
)

#: Скиллы для вывода из текста, когда ``<skills>`` пуст (best-effort).
_SKILL_KEYWORDS: tuple[str, ...] = (
    "python",
    "javascript",
    "typescript",
    "react",
    "react native",
    "vue",
    "angular",
    "node",
    "node.js",
    "next.js",
    "graphql",
    "django",
    "flask",
    "fastapi",
    "rails",
    "ruby",
    "php",
    "laravel",
    "java",
    "spring",
    "kotlin",
    "swift",
    "ios",
    "android",
    "flutter",
    "golang",
    "go",
    "rust",
    "c#",
    "c++",
    ".net",
    "sql",
    "postgresql",
    "mysql",
    "mongodb",
    "redis",
    "docker",
    "kubernetes",
    "terraform",
    "aws",
    "gcp",
    "azure",
    "linux",
    "git",
    "figma",
    "sketch",
    "seo",
    "salesforce",
    "shopify",
    "wordpress",
    "excel",
    "tableau",
    "power bi",
    "machine learning",
    "css",
    "html",
    "kafka",
    "rabbitmq",
    "elasticsearch",
    "selenium",
    "jest",
)


class WeWorkRemotelyError(RuntimeError):
    """Ошибка ленты/страницы We Work Remotely (не XML, HTTP != 200, пустая выдача)."""


# --- вспомогательные функции -------------------------------------------------


def _as_text(value: object, *, limit: int | None = None) -> str | None:
    """Строковое значение: None/пусто → None, длинное — обрезается."""
    if value is None:
        return None
    result = str(value).strip()
    if not result:
        return None
    return result[:limit] if limit else result


def _strip_html(fragment: str | None) -> str | None:
    """Плоский текст из HTML-фрагмента (переносы строк из блочных тегов)."""
    if not fragment:
        return None
    text = re.sub(r"<br\s*/?>", "\n", fragment, flags=re.IGNORECASE)
    text = re.sub(r"</p>|</li>|</h[1-6]>|</div>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    text = html_lib.unescape(text)
    lines = [line.strip() for line in text.splitlines()]
    result = "\n".join(line for line in lines if line)
    return result or None


def split_company_title(title: str | None) -> tuple[str | None, str | None]:
    """Разобрать заголовок вида «Company: Role» → (компания, роль).

    Компания — всё до первого двоеточия; без двоеточия компания неизвестна
    (None), роль — весь заголовок.
    """
    value = _as_text(title)
    if not value:
        return None, None
    company, sep, role = value.partition(":")
    if not sep:
        return None, value
    company = company.strip()
    role = role.strip()
    return (company or None), (role or value)


def external_id_for_url(url: str | None) -> str | None:
    """Детерминированный внешний id вакансии из её ссылки.

    Слаг последнего сегмента ``/remote-jobs/<slug>`` приводится к
    ``sha256[:24]``: слаги длиннее 32 символов, а ``hh_vacancy_id`` —
    VARCHAR(32) (docs/02 §3.3). Один и тот же слаг в ``<guid>`` ленты и в
    ``href`` страницы даёт одинаковый id.
    """
    value = _as_text(url)
    if not value:
        return None
    path = urlsplit(value).path.rstrip("/")
    match = re.search(r"/remote-jobs/([^/]+)$", path)
    if not match:
        return None
    slug = match.group(1)
    if not slug:
        return None
    return hashlib.sha256(slug.encode("utf-8")).hexdigest()[:24]


def feed_url_for(category: str | None = None) -> str:
    """RSS-лента категории (или общая лента, если категория неизвестна).

    Принимается ключ («programming») либо название категории («Programming»)
    без учёта регистра.
    """
    key = (category or "").strip().casefold()
    if not key:
        return RSS_URL
    for slug, (_path, display) in CATEGORY_PAGES.items():
        if key in (slug, display.casefold()):
            return f"{HOME_URL}{CATEGORY_PAGES[slug][0]}.rss"
    return RSS_URL


def listing_url_for(feed_url: str) -> str:
    """Страница выдачи для HTML-fallback: лента → её страница без ``.rss``."""
    value = (feed_url or "").strip()
    if not value:
        return LISTING_URL
    if value == RSS_URL:
        return LISTING_URL
    if value.endswith(".rss"):
        return value[: -len(".rss")]
    return value


def category_for_listing(listing_url: str | None) -> str | None:
    """Название категории по пути её страницы (для карточек HTML-fallback)."""
    path = urlsplit(str(listing_url or "")).path.rstrip("/")
    if not path:
        return None
    for _slug, (page_path, display) in CATEGORY_PAGES.items():
        if path == page_path.rstrip("/"):
            return display
    return None


def parse_rss(xml_text: str) -> list[dict]:
    """Разобрать RSS We Work Remotely в сырые карточки.

    Args:
        xml_text: тело ленты (RSS 2.0: ``<title>`` вида «Company: Role»,
            ``<region>``/``<country>``/``<state>``, ``<skills>``,
            ``<category>``, ``<type>``, ``<pubDate>``, ``<guid>``/``<link>``,
            HTML-``<description>``).

    Returns:
        list[dict]: сырые элементы ленты в порядке выдачи.

    Raises:
        WeWorkRemotelyError: тело не является корректным XML — сигнал
            включить HTML-fallback (docs/04 §10.1, аналог RemoteOKAPIError).
    """
    if not (xml_text or "").strip():
        raise WeWorkRemotelyError("Пустая RSS-лента weworkremotely.com")
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise WeWorkRemotelyError(f"RSS не разобрана: {exc}") from exc

    # Корень обязан быть <rss> (или <feed>): иначе это не лента, а, например,
    # страница ошибки/капча, которая формально является валидным XML.
    root_tag = root.tag.rsplit("}", 1)[-1].casefold()
    if root_tag not in ("rss", "feed"):
        raise WeWorkRemotelyError(f"Ожидалась RSS-лента, получен элемент <{root.tag}>")

    jobs: list[dict] = []
    for node in root.iter("item"):
        title = _as_text(node.findtext("title"))
        link = _as_text(node.findtext("link"))
        guid = _as_text(node.findtext("guid"))
        url = link or guid
        if not title or not url:
            continue
        jobs.append(
            {
                "title": title,
                "url": url,
                "guid": guid or url,
                "region": _as_text(node.findtext("region")),
                "country": _as_text(node.findtext("country")),
                "state": _as_text(node.findtext("state")),
                "skills": _as_text(node.findtext("skills")),
                "category": _as_text(node.findtext("category")),
                "type": _as_text(node.findtext("type")),
                "published_at": _as_text(node.findtext("pubDate")),
                "description_html": _as_text(node.findtext("description")),
            }
        )
    return jobs


def parse_html_jobs(html_text: str) -> list[dict]:
    """Разобрать HTML-выдачу weworkremotely.com в сырые карточки (best-effort).

    Стратегия: якоря ``/remote-jobs/<slug>`` с разметкой ``new-listing__*``
    (заголовок, компания, HQ, метки стран/занятости). Порядок выдачи
    сохраняется, дубли по id отбрасываются; служебные ссылки («Post a job»
    и т.п.) без заголовка роли пропускаются.
    """
    jobs: list[dict] = []
    seen: set[str] = set()
    for match in _LISTING_ANCHOR_RE.finditer(html_text or ""):
        slug = match.group("slug").strip()
        body = match.group("body")
        title_match = _TITLE_TEXT_RE.search(body)
        if not slug or not title_match:
            continue  # навигация/CTA — не карточка вакансии
        title = _strip_html(title_match.group("title"))
        if not title:
            continue
        url = f"{HOME_URL}/remote-jobs/{slug}"
        external_id = external_id_for_url(url)
        if not external_id or external_id in seen:
            continue
        seen.add(external_id)

        company_match = _COMPANY_RE.search(body)
        hq_match = _HQ_INNER_RE.search(body)
        hq = None
        if hq_match:
            hq = _strip_html(_HQ_LABEL_RE.sub("", hq_match.group("hq")))
        labels = [
            text
            for text in (_strip_html(m) for m in _CATEGORY_ITEM_RE.findall(body))
            if text
        ]
        employment = next(
            (label for label in labels if label.casefold() in _EMPLOYMENT_TYPES), None
        )
        jobs.append(
            {
                "url": url,
                "title": title,
                "company_name": _strip_html(company_match.group("company"))
                if company_match
                else None,
                "region": _region_from_labels(labels, hq),
                "employment_form": employment,
            }
        )
    return jobs


def _region_from_labels(labels: list[str], hq: str | None) -> str | None:
    """Регион карточки HTML-выдачи из меток со странами (best-effort).

    Страны помечены эмодзи-флагом: много стран → «Anywhere in the World»,
    одна — её название, несколько — первые три через запятую; без стран —
    HQ компании (штаб-квартира), затем None.
    """
    countries = [
        _FLAG_RE.sub("", label).strip() for label in labels if _FLAG_RE.search(label)
    ]
    countries = [country for country in countries if country]
    if len(countries) >= 15:
        return "Anywhere in the World"
    if len(countries) == 1:
        return countries[0]
    if countries:
        head = ", ".join(countries[:3])
        return f"{head}…" if len(countries) > 3 else head
    return hq


def _parse_skills(raw: object) -> list[str]:
    """Список скиллов из ``<skills>`` (строка через запятую/список)."""
    if isinstance(raw, (list, tuple, set)):
        return [item for item in (_as_text(value) for value in raw) if item]
    value = _as_text(raw)
    if not value:
        return []
    return [part.strip() for part in re.split(r"[,;|]", value) if part.strip()]


def _infer_skills(text: str | None) -> list[str]:
    """Вывести скиллы из текста по словарю (best-effort, порядок словаря)."""
    lowered = (text or "").casefold()
    if not lowered:
        return []
    found: list[str] = []
    for skill in _SKILL_KEYWORDS:
        pattern = rf"(?<![\w]){re.escape(skill)}(?![\w])"
        if re.search(pattern, lowered) and skill not in found:
            found.append(skill)
    return found


def _fetch_html(url: str, *, timeout: float = 20.0, impersonate: str = "chrome") -> str:
    """Лёгкий GET через curl_cffi (docs/04 §2 п.1) — HTML-fallback WWR.

    Вынесен в модульную функцию, чтобы тесты подменяли его без сети.
    """
    from curl_cffi import requests as curl_requests

    response = curl_requests.get(url, impersonate=impersonate, timeout=timeout)
    status = int(getattr(response, "status_code", 200) or 200)
    if status != 200:
        raise WeWorkRemotelyError(f"HTTP {status} от {url}")
    return str(response.text or "")


def _as_words(value: object) -> list[str]:
    """Ключевые слова: строка через запятую/список → список casefold-слов."""
    if value is None:
        return []
    if isinstance(value, str):
        parts = re.split(r"[,;]", value)
    elif isinstance(value, (list, tuple, set)):
        parts = list(value)
    else:
        parts = [value]
    return [word.casefold() for word in (_as_text(part) for part in parts) if word]


def _as_int(value: object) -> int | None:
    """Целое из фильтра limit/max_results; мусор → None."""
    try:
        result = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return result if result > 0 else None


def _matches_filters(
    job: dict,
    keywords: list[str],
    category: str | None,
    region: str | None,
    employment: set[str],
) -> bool:
    """Проверить сырую карточку по фильтрам search (AND по ключевым словам)."""
    text = " ".join(
        str(part)
        for part in (
            job.get("title"),
            job.get("company_name"),
            job.get("description_html"),
            job.get("description_raw"),
            job.get("region") or job.get("country") or job.get("state"),
            job.get("skills"),
            job.get("category"),
        )
        if part
    ).casefold()
    if any(keyword not in text for keyword in keywords):
        return False
    job_category = _as_text(job.get("category"))
    if category and (job_category or "").casefold() != category.casefold():
        return False
    if region:
        area = _as_text(
            job.get("region") or job.get("country") or job.get("state") or job.get("area")
        )
        if not area or region.casefold() not in area.casefold():
            return False
    job_employment = _as_text(job.get("type") or job.get("employment_form"))
    if employment and job_employment and job_employment.casefold() not in employment:
        return False
    return True


# --- адаптер источника -------------------------------------------------------


class WeWorkRemotelyAdapter(BaseSourceAdapter):
    """Источник We Work Remotely: официальные RSS-ленты + HTML-fallback (docs/04 §10)."""

    #: Имя источника в SourceRegistry и в канонической схеме вакансии.
    source_name = "weworkremotely"

    def __init__(self, session: AntiBanSession | None = None) -> None:
        # Сессия та же, что у оркестратора: единые прогрев/паузы/прокси и
        # общая статистика трафика (docs/04 §3). Свою создаёт только при
        # прямом использовании адаптера вне оркестратора.
        self.http = session or AntiBanSession(fetcher=build_page_fetcher())
        # Прямые ссылки, замеченные в выдаче: external_id → url. Хук
        # build_vacancy_url не может восстановить slug из хеша id, поэтому
        # оба хука работают в паре на одном экземпляре адаптера (§10.2).
        self._url_by_id: dict[str, str] = {}

    # --- контракт BaseSourceAdapter (docs/04 §10.1) --------------------------

    async def search(self, filters: dict) -> list[dict]:
        """Собрать карточки выдачи: официальные RSS → HTML-fallback.

        Args:
            filters: ``feed_url``/``search_url`` (готовая ссылка ленты либо
                HTML-страницы выдачи), ``category`` (ключ или название
                категории — выбирает ленту категории), ``keywords`` (AND по
                заголовку, компании и описанию), ``region`` (подстрока
                региона), ``employment_forms``/``employment_form`` (форма
                занятости), ``max_results``/``limit`` (ограничение числа
                карточек).

        Returns:
            list[dict]: сырые карточки ``{source, external_id, url}`` в
            порядке выдачи, без дублей.
        """
        options = filters or {}
        keywords = _as_words(options.get("keywords"))
        # Категория нормализуется к названию из CATEGORY_PAGES — карточки
        # HTML-fallback получают её же (см. ``_fetch_html_jobs``).
        category = category_display(options.get("category"))
        region = _as_text(options.get("region"))
        employment_raw = options.get("employment_forms")
        if employment_raw is None:
            employment_raw = options.get("employment_form") or options.get("type")
        employment = {word.casefold() for word in _as_words(employment_raw)}
        limit = _as_int(options.get("max_results") or options.get("limit"))

        feed_url = _as_text(options.get("feed_url") or options.get("search_url"))
        if feed_url:
            feed_url = self.validate_search_url(feed_url)
        else:
            feed_url = self.build_search_url(category=category)

        if feed_url.endswith(".rss"):
            try:
                jobs = await self._fetch_rss(feed_url)
            except Exception as exc:  # noqa: BLE001 — любой сбой ленты включает fallback
                logger.warning("RSS %s недоступна (%s) — HTML-fallback", feed_url, exc)
                jobs = await self._fetch_html_jobs(
                    listing_url_for(feed_url), category=category
                )
        else:
            # Пользователь явно указал HTML-страницу выдачи — минуем RSS.
            jobs = await self._fetch_html_jobs(feed_url, category=category)

        results: list[dict] = []
        seen: set[str] = set()
        for job in jobs:
            if not isinstance(job, dict):
                continue
            external_id = external_id_for_url(job.get("url")) or _as_text(
                job.get("external_id")
            )
            if not external_id or external_id in seen:
                continue
            if not _matches_filters(job, keywords, category, region, employment):
                continue
            seen.add(external_id)
            card = dict(job)
            card["source"] = self.source_name
            card["external_id"] = external_id
            card["url"] = _as_text(job.get("url"))
            if card["url"]:
                self._remember(external_id, card["url"])
            results.append(card)
            if limit is not None and len(results) >= limit:
                break
        return results

    async def get_vacancy(self, external_id_or_url: str) -> dict | None:
        """Одна вакансия по внешнему id или прямой ссылке (docs/04 §5).

        Основной путь — общая RSS-лента (в элементе есть полное описание);
        при отказе/пустой ленте карточка запрашивается по прямой ссылке.
        None — вакансия не найдена/удалена на источнике.
        """
        value = _as_text(external_id_or_url)
        if not value:
            return None
        if value.startswith(("http://", "https://")):
            external_id = external_id_for_url(value)
            if not external_id:
                return None
            url = value
            self._remember(external_id, value)
        else:
            external_id = value[:32]
            url = self._url_by_id.get(external_id)

        try:
            items = await self._fetch_rss(RSS_URL)
        except Exception as exc:  # noqa: BLE001 — лента упала, пробуем карточку
            logger.debug("RSS недоступна для get_vacancy (%s) — карточка по ссылке", exc)
            items = None
        if items:
            for item in items:
                if external_id_for_url(item.get("url")) == external_id:
                    self._remember(external_id, item["url"])
                    return {
                        "source": self.source_name,
                        "external_id": external_id,
                        **item,
                    }
        if not url:
            # Лента здорова, но вакансии в ней нет; ссылки для карточки нет.
            return None

        response = await self.http.fetch(url)
        if response.status_code in _NOT_FOUND_STATUSES:
            return None  # docs/04 §5 → not_found
        if response.status_code != 200:
            logger.debug("Карточка %s: HTTP %s", url, response.status_code)
            return None
        fields = self.extract_fields(response.text)
        if not fields.get("title"):
            logger.debug("Карточка %s не разобрана (вёрстка/капча)", url)
            return None
        return {
            "source": self.source_name,
            "external_id": external_id,
            "url": url,
            **fields,
        }

    def normalize(self, raw: dict) -> dict:
        """Сырые данные WWR → каноническая схема вакансии (docs/04 §7).

        Заголовок «Company: Role» разбирается на компанию и роль; регион —
        из ``<region>`` (фолбэк ``<country>``/``<state>``); скиллы — из
        ``<skills>`` либо выводятся из текста. Плюс поля задачи:
        ``category``, ``region``, ``skills``, ``tags``, ``remote=True``.
        Зарплата в ленте WWR не публикуется — ``salary_*`` всегда None.
        """
        data = raw or {}
        url = _as_text(data.get("url") or data.get("link") or data.get("guid"))
        external_id = external_id_for_url(url) or _as_text(data.get("external_id"))
        if not external_id:
            raise ValueError("Vacancy missing external_id")
        external_id = external_id[:32]
        if url:
            self._remember(external_id, url)

        title_full = _as_text(data.get("title"), limit=512)
        company = _as_text(data.get("company_name") or data.get("company"), limit=512)
        role = _as_text(data.get("role") or data.get("position"), limit=512)
        if company is None or role is None:
            company_title, role_title = split_company_title(title_full)
            company = company or company_title
            role = role or role_title

        region = _as_text(data.get("region"))
        country = _as_text(data.get("country"))
        state = _as_text(data.get("state"))
        area = _as_text(
            region
            or country
            or state
            or data.get("area")
            or data.get("location")
            or data.get("hq"),
            limit=255,
        )

        skills = _parse_skills(data.get("skills"))
        if not skills:
            skills = _infer_skills(
                " ".join(
                    str(part)
                    for part in (
                        role,
                        title_full,
                        data.get("description_html") or data.get("description_raw"),
                    )
                    if part
                )
            )

        description_html = _as_text(data.get("description_html") or data.get("description"))
        description_raw = _as_text(data.get("description_raw")) or _strip_html(description_html)

        return {
            "source": self.source_name,
            "external_id": external_id,
            "url": url,
            "title": role,
            "company_name": company,
            "salary_from": None,
            "salary_to": None,
            "salary_currency": None,
            "experience": None,
            "employment_form": _as_text(
                data.get("employment_form") or data.get("type"), limit=64
            ),
            "work_format": "remote",
            "schedule": None,
            "area": area or "",
            "published_at": _as_text(
                data.get("published_at") or data.get("pubDate") or data.get("date")
            ),
            "description_raw": description_raw,
            "description_html": description_html,
            # специфика We Work Remotely
            "category": _as_text(data.get("category")),
            "region": region,
            "skills": skills,
            "tags": skills,
            "remote": True,
        }

    # --- хуки ParsingOrchestrator (docs/04 §10.2) ----------------------------

    def build_search_url(
        self,
        *,
        keywords: list[str] | None = None,
        category: str | None = None,
        **_kwargs: object,
    ) -> str:
        """Ссылка выдачи автопоиска (docs/04 §4.1): официальная RSS-лента.

        Лента не поддерживает поиск в URL — ``keywords``, формы занятости и
        регионы фильтруются в ``search``; ``category`` (ключ или название)
        выбирает ленту категории.
        """
        return feed_url_for(category)

    def validate_search_url(self, search_url: str) -> str:
        """Принимаются только ссылки выдачи weworkremotely.com (docs/04 §4.2)."""
        candidate = str(search_url).strip()
        parts = urlsplit(candidate)
        host = (parts.hostname or "").lower()
        if parts.scheme not in ("http", "https") or host not in _ALLOWED_HOSTS:
            raise ValueError(
                "Ссылка выдачи We Work Remotely должна быть на weworkremotely.com: "
                f"{candidate!r}"
            )
        return candidate

    def page_url(self, base_url: str, page: int) -> str:
        """Страница выдачи: у ленты пагинации нет, HTML — параметр ``page``."""
        if str(base_url).endswith(".rss"):
            return base_url
        index = max(0, int(page or 0))
        if index == 0:
            return base_url
        joiner = "&" if "?" in base_url else "?"
        return f"{base_url}{joiner}page={index + 1}"

    def parse_listing(self, html: str) -> tuple[list[str], bool]:
        """Разобрать выдачу: (external_id в порядке выдачи, есть следующая).

        Понимает оба пути адаптера: RSS-ленту (основной, тег ``<rss>`` в
        начале тела) и HTML-страницу категории (fallback). Id запоминаются
        вместе с ссылками — для ``build_vacancy_url``.
        """
        text = html or ""
        if "<rss" in text[:4096].casefold():
            try:
                items = parse_rss(text)
            except WeWorkRemotelyError as exc:
                logger.warning("parse_listing: лента не разобрана: %s", exc)
                return [], False
            has_next = False  # у RSS-ленты нет пагинации
        else:
            items = parse_html_jobs(text)
            has_next = bool(_NEXT_LINK_RE.search(text))

        external_ids: list[str] = []
        seen: set[str] = set()
        for item in items:
            external_id = external_id_for_url(item.get("url"))
            if not external_id or external_id in seen:
                continue
            seen.add(external_id)
            self._remember(external_id, item.get("url"))
            external_ids.append(external_id)
        return external_ids, has_next

    def build_vacancy_url(self, base_url: str, external_id: str) -> str:
        """Прямая ссылка карточки по id из выдачи.

        Id — хеш слага (см. ``external_id_for_url``), обратное преобразование
        невозможно, поэтому адаптер запоминает ссылки при разборе выдачи
        (``parse_listing``/``search``/``normalize``). Неизвестный id →
        ValueError: карточка не встречалась в выдаче.
        """
        key = _as_text(external_id)
        url = self._url_by_id.get(key or "")
        if not url:
            raise ValueError(
                f"Прямая ссылка We Work Remotely для id {external_id!r} неизвестна: "
                "карточка не встречалась в выдаче"
            )
        return url

    def extract_fields(self, html: str) -> dict:
        """Поля карточки WWR из HTML (docs/04 §7, только колонки vacancies)."""
        return extract_card_fields(html)

    # --- вспомогательное -----------------------------------------------------

    def _remember(self, external_id: str | None, url: str | None) -> None:
        """Запомнить прямую ссылку карточки для хука ``build_vacancy_url``."""
        key = _as_text(external_id)
        link = _as_text(url)
        if key and link:
            self._url_by_id.setdefault(key, link)

    async def _fetch_rss(self, feed_url: str) -> list[dict]:
        """Официальная лента через AntiBanSession (основной путь, §2/§10.1).

        Raises:
            WeWorkRemotelyError: HTTP != 200, не XML или пустая лента —
                сигнал включить HTML-fallback.
        """
        response = await self.http.fetch(feed_url)
        if response.status_code != 200:
            raise WeWorkRemotelyError(f"HTTP {response.status_code} от {feed_url}")
        items = parse_rss(response.text)
        if not items:
            raise WeWorkRemotelyError(f"В ленте {feed_url} нет элементов")
        return items

    async def _fetch_html_jobs(
        self, listing_url: str, category: str | None = None
    ) -> list[dict]:
        """Fallback: HTML-страница выдачи — curl_cffi, затем AntiBanSession.

        Сначала лёгкий GET через ``_fetch_html`` (curl_cffi, docs/04 §2 п.1);
        при его сбое — надёжный путь через ``AntiBanSession`` (та же цепочка
        curl_cffi → Playwright+stealth, что у оркестратора). Категория
        страницы дозаполняется в карточки, если её нет в разметке.
        """
        try:
            html_text = await asyncio.to_thread(_fetch_html, listing_url)
        except Exception as exc:  # noqa: BLE001 — curl_cffi не дал контента
            logger.debug(
                "curl_cffi не дал HTML %s (%s) — пробую AntiBanSession", listing_url, exc
            )
            response = await self.http.fetch(listing_url)
            if response.status_code in _NOT_FOUND_STATUSES:
                return []
            if response.status_code != 200:
                raise WeWorkRemotelyError(
                    f"HTTP {response.status_code} от {listing_url}"
                ) from exc
            html_text = response.text

        jobs = parse_html_jobs(html_text)
        if not jobs:
            logger.warning("HTML-fallback %s: карточки в выдаче не найдены", listing_url)
        category_name = category_display(category) or category_for_listing(listing_url)
        if category_name:
            for job in jobs:
                job.setdefault("category", category_name)
        return jobs


# --- разбор карточки вакансии ------------------------------------------------


#: ``<title>`` карточки вида «Remote Роль at Компания» (фолбэк источника).
_PAGE_TITLE_RE = re.compile(r"^Remote\s+(?P<role>.+?)\s+at\s+(?P<company>.+)$", re.DOTALL)


def category_display(category: str | None) -> str | None:
    """Название категории по ключу или названию (без учёта регистра).

    Неизвестная категория возвращается как есть — фильтрация по ней даст
    пустую выдачу, как и в RSS-пути.
    """
    key = (category or "").strip().casefold()
    if not key:
        return None
    for _slug, (_path, display) in CATEGORY_PAGES.items():
        if key in (_slug, display.casefold()):
            return display
    return _as_text(category)


def _card_match(pattern: re.Pattern[str], text: str) -> str | None:
    """Содержимое именованной группы первого совпадения (или None)."""
    match = pattern.search(text)
    if not match:
        return None
    group = match.lastgroup
    return match.group(group) if group else None


def extract_card_fields(html_text: str) -> dict:
    """Поля карточки вакансии WWR из HTML (docs/04 §7, best-effort).

    Возвращаются только контент-колонки vacancies: ``_ingest`` оркестратора
    строит из них запись в БД, а ``source``/``external_id``/``url`` задаёт
    оркестратор. Источники: h1 hero-заголовка, ``<title>`` вида
    «Remote Роль at Компания», hero-описание, ``/company/<slug>`` и блок
    ``lis-container__job__content__description``.
    """
    text = html_text or ""
    fields: dict[str, object] = {}

    page_title = _strip_html(_card_match(_CARD_PAGE_TITLE_RE, text))
    page_role = None
    page_company = None
    if page_title:
        page_title = page_title.split("|")[0].strip()
        page_match = _PAGE_TITLE_RE.match(page_title)
        if page_match:
            page_role = page_match.group("role").strip()
            page_company = page_match.group("company").strip()

    title = _strip_html(_card_match(_CARD_TITLE_RE, text)) or page_role
    if title:
        fields["title"] = title

    company = _strip_html(_card_match(_CARD_COMPANY_RE, text)) or page_company
    if not company:
        slug = _card_match(_CARD_COMPANY_SLUG_RE, text)
        if slug:
            company = " ".join(part.capitalize() for part in slug.split("-"))
    if company:
        fields["company_name"] = company

    description = _card_match(_CARD_DESC_RE, text)
    if description:
        fields["description_html"] = description
        description_raw = _strip_html(description)
        if description_raw:
            fields["description_raw"] = description_raw

    # WWR публикует только удалённые вакансии; зарплата/дата на карточке нет.
    fields["work_format"] = "remote"
    return fields













