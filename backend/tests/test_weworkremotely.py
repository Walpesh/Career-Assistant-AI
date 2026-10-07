"""Тесты адаптера We Work Remotely: RSS, normalize, fallback и SourceRegistry.

Покрывают docs/04_PARSING_RULES.md §10 (адаптер источника, SourceRegistry):

- ``parse_rss`` — разбор официальной ленты: «Company: Role», регион,
  скиллы, категория, форма занятости, дата и описание;
- ``normalize`` — маппинг в каноническую схему §7 (source, external_id,
  title, company_name, area, employment_form, description_*) плюс поля
  задачи category/region/skills/tags/remote;
- ``search`` — фильтры по ключевым словам/категории/региону/занятости,
  дедупликация, лимит;
- fallback — при отказе/непригодности RSS разбор HTML-страницы категории
  через curl_cffi, а при его сбое — через AntiBanSession (Playwright);
- ``get_vacancy``, хуки оркестратора (build_search_url/parse_listing/
  build_vacancy_url/extract_fields) и регистрация в ``default_registry()``.

Сеть не используется: лента и страницы подменяются FakeSession
(FetchResponse), а ``_fetch_html`` (curl_cffi) — monkeypatch'ем, поэтому
тесты проходят без внешних HTTP-запросов.
"""

from __future__ import annotations

import hashlib

import pytest
from app.modules.anti_ban.session import FetchResponse
from app.modules.parsing.sources import default_registry
from app.modules.parsing.sources import weworkremotely as wwr_module
from app.modules.parsing.sources.weworkremotely import (
    HOME_URL,
    LISTING_URL,
    RSS_URL,
    WeWorkRemotelyAdapter,
    WeWorkRemotelyError,
    external_id_for_url,
    parse_html_jobs,
    parse_rss,
    split_company_title,
)

# --- фикстуры RSS ------------------------------------------------------------

#: sha256(slug)[:24] — так external_id считает адаптер.
ACME_ID = "020ee6bdc40f69e02adcc6fe"
GLOBEX_ID = "600ff9d31e7aa9882d31eda0"
FRONTEND_ID = "217195cecdb1a7e9e87a5a85"
BACKEND_ID = "f585c96a9e181f818211d3ae"
SAMSARA_ID = "6220953276c396c76e743570"

RSS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:media="http://search.yahoo.com/mrss">
  <channel>
    <title>We Work Remotely: Remote jobs in design, programming and more</title>
    <link>https://weworkremotely.com/remote-jobs.rss</link>
    <item>
      <title>Acme Corp: Senior Python Engineer</title>
      <region>Anywhere in the World</region>
      <country></country>
      <state></state>
      <skills>Python, Django</skills>
      <category>Programming</category>
      <type>Full-Time</type>
      <description>&lt;p&gt;Build &lt;b&gt;Python&lt;/b&gt; APIs with Django.&lt;/p&gt;</description>
      <pubDate>Wed, 07 Oct 2026 10:00:00 +0000</pubDate>
      <guid>https://weworkremotely.com/remote-jobs/acme-corp-senior-python-engineer</guid>
      <link>https://weworkremotely.com/remote-jobs/acme-corp-senior-python-engineer</link>
    </item>
    <item>
      <title>Globex: DevOps Engineer</title>
      <region></region>
      <country>Germany</country>
      <state></state>
      <skills></skills>
      <category>Devops and Sysadmin</category>
      <type>Contract</type>
      <description>&lt;p&gt;Operate Docker and Kubernetes clusters.&lt;/p&gt;</description>
      <pubDate>Tue, 06 Oct 2026 09:30:00 +0000</pubDate>
      <guid>https://weworkremotely.com/remote-jobs/globex-devops-engineer</guid>
      <link>https://weworkremotely.com/remote-jobs/globex-devops-engineer</link>
    </item>
    <item>
      <title>Initech: Frontend Developer</title>
      <region>Europe</region>
      <country></country>
      <state></state>
      <skills>React, TypeScript</skills>
      <category>Front-End Programming</category>
      <type>Full-Time</type>
      <description>&lt;p&gt;Craft interfaces with React.&lt;/p&gt;</description>
      <pubDate>Mon, 05 Oct 2026 08:00:00 +0000</pubDate>
      <guid>https://weworkremotely.com/remote-jobs/initech-frontend-developer</guid>
      <link>https://weworkremotely.com/remote-jobs/initech-frontend-developer</link>
    </item>
  </channel>
</rss>
"""

CATEGORY_FEED_URL = f"{HOME_URL}/categories/remote-programming-jobs.rss"
CATEGORY_LISTING_URL = f"{HOME_URL}/categories/remote-programming-jobs"

# --- фикстуры HTML (fallback и карточка) -------------------------------------

LISTING_HTML = """
<html><body>
<section id="header-top-bar">
  <nav><a href="/remote-jobs/find-your-plan?utm_source=nav">Post a job</a></nav>
</section>
<section class="jobs">
  <a class="listing-link--unlocked" href="/remote-jobs/initech-backend-developer">
    <div class=" new-listing paid-logo ">
      <div class="new-listing__header">
        <h3 class="new-listing__header__title">
          <span class="new-listing__header__title__text">Backend Developer</span>
        </h3>
      </div>
      <p class="new-listing__company-name"> Initech <img alt="" src="/logo.svg"/></p>
      <p class="new-listing__company-headquarters">
        <span class="new-listing__company-headquarters__label"> HQ:</span> Berlin, Germany
      </p>
      <div class="new-listing__categories">
        <p class="new-listing__categories__category"> Contract </p>
        <p class="new-listing__categories__category"> \U0001F1E9\U0001F1EA Germany </p>
      </div>
    </div>
  </a>
  <a class="listing-link--unlocked" href="/remote-jobs/samsara-staff-software-engineer">
    <div class=" new-listing ">
      <div class="new-listing__header">
        <h3 class="new-listing__header__title">
          <span class="new-listing__header__title__text">Staff Software Engineer</span>
        </h3>
      </div>
      <p class="new-listing__company-name"> Samsara </p>
      <div class="new-listing__categories">
        <p class="new-listing__categories__category"> Full-Time </p>
        <p class="new-listing__categories__category"> \U0001F1E9\U0001F1EA Germany </p>
        <p class="new-listing__categories__category"> \U0001F1FA\U0001F1F8 United States </p>
        <p class="new-listing__categories__category"> \U0001F1F5\U0001F1F1 Poland </p>
        <p class="new-listing__categories__category"> \U0001F1EC\U0001F1E7 United Kingdom </p>
        <p class="new-listing__categories__category"> \U0001F1EB\U0001F1F7 France </p>
        <p class="new-listing__categories__category"> \U0001F1EA\U0001F1F8 Spain </p>
        <p class="new-listing__categories__category"> \U0001F1E8\U0001F1E6 Canada </p>
        <p class="new-listing__categories__category"> \U0001F1E6\U0001F1FA Australia </p>
        <p class="new-listing__categories__category"> \U0001F1EF\U0001F1F5 Japan </p>
        <p class="new-listing__categories__category"> \U0001F1E7\U0001F1F7 Brazil </p>
        <p class="new-listing__categories__category"> \U0001F1EE\U0001F1F3 India </p>
        <p class="new-listing__categories__category"> \U0001F1F3\U0001F1F1 Netherlands </p>
        <p class="new-listing__categories__category"> \U0001F1F8\U0001F1EA Sweden </p>
        <p class="new-listing__categories__category"> \U0001F1FA\U0001F1E6 Ukraine </p>
        <p class="new-listing__categories__category"> \U0001F1F5\U0001F1F9 Portugal </p>
      </div>
    </div>
  </a>
</section>
<a rel="next" href="/categories/remote-programming-jobs?page=2">Next</a>
</body></html>
"""

CARD_HTML = """
<html><head>
<title>Remote Backend Developer at Initech</title>
<link rel="canonical" href="https://weworkremotely.com/remote-jobs/initech-backend-developer"/>
</head><body>
<section class="lis-container__header">
  <div class="lis-container__header__hero">
    <a href="/company/initech"><div class="lis-container__header__hero__company-logo"></div></a>
    <div class="lis-container__header__hero__company-info">
      <h1 class="lis-container__header__hero__company-info__title"> Backend Developer </h1>
      <div class="lis-container__header__hero__company-info__description">
        <div><a href="https://initech.example">Initech</a> — builds stuff.<br><br></div>
      </div>
    </div>
  </div>
</section>
<section class="lis-container__job">
  <div class="lis-container__job__content">
    <div class="lis-container__job__content__description">
      <p>We build <b>APIs</b> with Python.</p>
      <p>Great opportunity for experienced developers.</p>
    </div>
  </div>
</section>
</body></html>
"""


class FakeSession:
    """AntiBanSession с подменённым fetch: без сети, прогрева и пауз.

    Значением в ``pages`` может быть FetchResponse либо исключение —
    тогда fetch его выбросит (сценарий «RSS упала»).
    """

    def __init__(self, pages: dict | None = None):
        self.pages = pages or {}
        self.requested: list[str] = []

    async def fetch(self, url: str, **_kwargs) -> FetchResponse:
        self.requested.append(url)
        result = self.pages.get(url, FetchResponse(status_code=404, text="", url=url))
        if isinstance(result, Exception):
            raise result
        return result


def _rss_response(text: str = RSS_XML, status: int = 200) -> FetchResponse:
    return FetchResponse(status_code=status, text=text, url=RSS_URL)


def _adapter(pages: dict | None = None) -> WeWorkRemotelyAdapter:
    return WeWorkRemotelyAdapter(session=FakeSession(pages))


# --- parse_rss / split_company_title / external_id ---------------------------


def test_parse_rss_extracts_company_title_region_and_details():
    """Официальная лента: «Company: Role», регион, скиллы, категория, дата."""
    items = parse_rss(RSS_XML)

    assert len(items) == 3
    first = items[0]
    assert first["title"] == "Acme Corp: Senior Python Engineer"
    assert first["url"] == (
        "https://weworkremotely.com/remote-jobs/acme-corp-senior-python-engineer"
    )
    assert first["guid"] == first["url"]
    assert first["region"] == "Anywhere in the World"
    assert first["skills"] == "Python, Django"
    assert first["category"] == "Programming"
    assert first["type"] == "Full-Time"
    assert first["published_at"] == "Wed, 07 Oct 2026 10:00:00 +0000"
    assert first["description_html"] == "<p>Build <b>Python</b> APIs with Django.</p>"

    second = items[1]
    assert second["region"] is None  # пустой <region>
    assert second["country"] == "Germany"
    assert second["skills"] is None  # пустой <skills>
    assert second["category"] == "Devops and Sysadmin"
    assert second["type"] == "Contract"


def test_parse_rss_invalid_xml_raises():
    """Не XML / пустая лента → WeWorkRemotelyError (сигнал включить fallback)."""
    with pytest.raises(WeWorkRemotelyError):
        parse_rss("<html>just a moment</html>")
    with pytest.raises(WeWorkRemotelyError):
        parse_rss("")


def test_split_company_title():
    """Заголовок «Company: Role» → (компания, роль); без двоеточия — роль одна."""
    assert split_company_title("Acme Corp: Senior Python Engineer") == (
        "Acme Corp",
        "Senior Python Engineer",
    )
    assert split_company_title("Just a Role") == (None, "Just a Role")
    assert split_company_title(None) == (None, None)


def test_external_id_is_deterministic_and_fits_db():
    """Один хеш и для guid ленты, и для href страницы; длина ≤ 32 (docs/02 §3.3)."""
    guid = "https://weworkremotely.com/remote-jobs/acme-corp-senior-python-engineer"
    expected = hashlib.sha256(b"acme-corp-senior-python-engineer").hexdigest()[:24]

    assert external_id_for_url(guid) == expected == ACME_ID
    assert external_id_for_url("/remote-jobs/acme-corp-senior-python-engineer") == ACME_ID
    assert len(external_id_for_url(guid) or "") <= 32
    assert external_id_for_url("https://weworkremotely.com/categories/remote-design-jobs") is None


# --- normalize ---------------------------------------------------------------


def test_normalize_rss_item_to_canonical_schema():
    """Элемент ленты → каноническая схема docs/04 §7 + поля задачи WWR."""
    adapter = _adapter()
    item = parse_rss(RSS_XML)[0]
    result = adapter.normalize(item)

    assert result["source"] == "weworkremotely"
    assert result["external_id"] == ACME_ID
    assert result["url"] == item["url"]
    assert result["title"] == "Senior Python Engineer"
    assert result["company_name"] == "Acme Corp"
    assert result["area"] == "Anywhere in the World"
    assert result["employment_form"] == "Full-Time"
    assert result["work_format"] == "remote"
    assert result["published_at"] == "Wed, 07 Oct 2026 10:00:00 +0000"
    assert result["description_html"] == "<p>Build <b>Python</b> APIs with Django.</p>"
    assert result["description_raw"] == "Build Python APIs with Django."
    # канонические поля, которых WWR не публикует
    assert result["salary_from"] is None
    assert result["salary_to"] is None
    assert result["salary_currency"] is None
    assert result["experience"] is None
    assert result["schedule"] is None
    # специфика источника
    assert result["category"] == "Programming"
    assert result["region"] == "Anywhere in the World"
    assert result["skills"] == ["Python", "Django"]
    assert result["tags"] == result["skills"]
    assert result["remote"] is True


def test_normalize_infers_skills_and_falls_back_to_country():
    """Пустые <skills>/<region> → скиллы из описания, area из <country>."""
    result = _adapter().normalize(parse_rss(RSS_XML)[1])

    assert result["external_id"] == GLOBEX_ID
    assert result["title"] == "DevOps Engineer"
    assert result["company_name"] == "Globex"
    assert result["area"] == "Germany"
    assert result["employment_form"] == "Contract"
    assert result["skills"] == ["docker", "kubernetes"]
    assert result["category"] == "Devops and Sysadmin"


def test_normalize_missing_external_id_raises():
    """Без ссылки и id нормализация невозможна — ValueError (как у remotive)."""
    with pytest.raises(ValueError):
        _adapter().normalize({"title": "No URL: Role"})


# --- HTML-fallback: разбор выдачи --------------------------------------------


def test_parse_html_jobs_extracts_company_region_and_employment():
    """HTML-выдача: компания, роль, регион из флагов, форма занятости."""
    jobs = parse_html_jobs(LISTING_HTML)

    assert [job["url"] for job in jobs] == [
        f"{HOME_URL}/remote-jobs/initech-backend-developer",
        f"{HOME_URL}/remote-jobs/samsara-staff-software-engineer",
    ]
    assert jobs[0]["title"] == "Backend Developer"
    assert jobs[0]["company_name"] == "Initech"
    assert jobs[0]["region"] == "Germany"  # одна страна-флаг
    assert jobs[0]["employment_form"] == "Contract"
    assert jobs[1]["region"] == "Anywhere in the World"  # 15+ стран
    assert jobs[1]["employment_form"] == "Full-Time"
    # служебные ссылки («Post a job») без роли не становятся вакансиями
    assert all("find-your-plan" not in job["url"] for job in jobs)


# --- search через RSS --------------------------------------------------------


async def test_search_via_rss():
    """Основной путь: официальная лента через AntiBanSession."""
    adapter = _adapter({RSS_URL: _rss_response()})
    cards = await adapter.search({})

    assert adapter.http.requested == [RSS_URL]
    assert [card["external_id"] for card in cards] == [ACME_ID, GLOBEX_ID, FRONTEND_ID]
    for card in cards:
        assert card["source"] == "weworkremotely"
        assert card["url"].startswith("https://weworkremotely.com/remote-jobs/")
        assert external_id_for_url(card["url"]) == card["external_id"]


async def test_search_filters_keywords_category_region_employment_and_limit():
    """Фильтры: keywords (AND), category, region, employment_forms, limit."""
    adapter = _adapter({RSS_URL: _rss_response(), CATEGORY_FEED_URL: _rss_response()})

    python_cards = await adapter.search({"keywords": ["python"]})
    assert [card["external_id"] for card in python_cards] == [ACME_ID]

    # строка через запятую нормализуется в список слов
    both = await adapter.search({"keywords": "senior, acme"})
    assert [card["external_id"] for card in both] == [ACME_ID]

    react = await adapter.search({"keywords": ["react"]})
    assert [card["external_id"] for card in react] == [FRONTEND_ID]

    # категория выбирает ленту категории и фильтрует карточки
    programming = await adapter.search({"category": "programming"})
    assert adapter.http.requested[-1] == CATEGORY_FEED_URL
    assert [card["external_id"] for card in programming] == [ACME_ID]

    germany = await adapter.search({"region": "germany"})
    assert [card["external_id"] for card in germany] == [GLOBEX_ID]

    contracts = await adapter.search({"employment_forms": ["contract"]})
    assert [card["external_id"] for card in contracts] == [GLOBEX_ID]

    limited = await adapter.search({"max_results": 1})
    assert [card["external_id"] for card in limited] == [ACME_ID]


# --- HTML-fallback (curl_cffi → AntiBanSession) ------------------------------


async def test_search_falls_back_to_html_when_rss_fails(monkeypatch):
    """Лента недоступна → HTML-страница выдачи через curl_cffi (без сети)."""
    fetched: list[str] = []

    def fake_fetch_html(url: str, **_kwargs) -> str:
        fetched.append(url)
        return LISTING_HTML

    monkeypatch.setattr(wwr_module, "_fetch_html", fake_fetch_html)
    adapter = _adapter({RSS_URL: RuntimeError("connection reset")})

    cards = await adapter.search({})

    assert adapter.http.requested == [RSS_URL]
    assert fetched == [LISTING_URL]
    assert [card["external_id"] for card in cards] == [BACKEND_ID, SAMSARA_ID]
    for card in cards:
        assert card["source"] == "weworkremotely"

    normalized = adapter.normalize(cards[0])
    assert normalized["title"] == "Backend Developer"
    assert normalized["company_name"] == "Initech"
    assert normalized["area"] == "Germany"
    assert normalized["employment_form"] == "Contract"
    assert normalized["remote"] is True

    assert adapter.normalize(cards[1])["area"] == "Anywhere in the World"


async def test_search_rss_not_xml_falls_back_to_html(monkeypatch):
    """Лента отдаёт не XML (капча/страница ошибки) → тот же HTML-fallback."""
    monkeypatch.setattr(
        wwr_module, "_fetch_html", lambda *_args, **_kwargs: LISTING_HTML
    )
    adapter = _adapter({RSS_URL: _rss_response(text="<html>just a moment</html>")})

    cards = await adapter.search({})

    assert [card["external_id"] for card in cards] == [BACKEND_ID, SAMSARA_ID]


async def test_search_falls_back_to_session_when_curl_cffi_fails(monkeypatch):
    """curl_cffi не дал контента → надёжный путь через AntiBanSession."""

    def broken(_url: str, **_kwargs) -> str:
        raise WeWorkRemotelyError("curl_cffi blocked")

    monkeypatch.setattr(wwr_module, "_fetch_html", broken)
    adapter = _adapter(
        {
            RSS_URL: RuntimeError("connection reset"),
            LISTING_URL: FetchResponse(
                status_code=200, text=LISTING_HTML, url=LISTING_URL
            ),
        }
    )

    cards = await adapter.search({})

    assert adapter.http.requested == [RSS_URL, LISTING_URL]
    assert [card["external_id"] for card in cards] == [BACKEND_ID, SAMSARA_ID]


async def test_fallback_cards_inherit_category_from_listing(monkeypatch):
    """Карточки HTML-fallback получают категорию своей страницы выдачи."""
    monkeypatch.setattr(
        wwr_module, "_fetch_html", lambda *_args, **_kwargs: LISTING_HTML
    )
    adapter = _adapter({CATEGORY_FEED_URL: RuntimeError("down")})

    cards = await adapter.search({"category": "programming"})

    assert cards
    assert all(card["category"] == "Programming" for card in cards)


async def test_search_with_html_search_url_skips_rss(monkeypatch):
    """Прямая ссылка на HTML-страницу выдачи (group-режим) — минуем RSS."""
    fetched: list[str] = []

    def fake_fetch_html(url: str, **_kwargs) -> str:
        fetched.append(url)
        return LISTING_HTML

    monkeypatch.setattr(wwr_module, "_fetch_html", fake_fetch_html)
    adapter = _adapter({RSS_URL: _rss_response()})

    cards = await adapter.search({"search_url": LISTING_URL})

    assert adapter.http.requested == []
    assert fetched == [LISTING_URL]
    assert [card["external_id"] for card in cards] == [BACKEND_ID, SAMSARA_ID]


# --- get_vacancy -------------------------------------------------------------


async def test_get_vacancy_by_url_returns_rss_item():
    """Прямая ссылка → элемент ленты (описание уже в RSS, без карточки)."""
    adapter = _adapter({RSS_URL: _rss_response()})
    url = "https://weworkremotely.com/remote-jobs/acme-corp-senior-python-engineer"

    result = await adapter.get_vacancy(url)

    assert result is not None
    assert result["source"] == "weworkremotely"
    assert result["external_id"] == ACME_ID
    assert result["title"] == "Acme Corp: Senior Python Engineer"  # сырой заголовок
    assert "Django" in (result["description_html"] or "")
    assert adapter.http.requested == [RSS_URL]  # карточка не запрашивалась


async def test_get_vacancy_by_id_falls_back_to_card_html():
    """Лента упала → карточка по ссылке из кэша выдачи, разбор HTML."""
    card_url = f"{HOME_URL}/remote-jobs/initech-backend-developer"
    adapter = _adapter(
        {
            RSS_URL: RuntimeError("connection reset"),
            card_url: FetchResponse(status_code=200, text=CARD_HTML, url=card_url),
        }
    )
    adapter.parse_listing(LISTING_HTML)  # оркестратор сначала разбирает выдачу

    result = await adapter.get_vacancy(BACKEND_ID)

    assert result is not None
    assert result["external_id"] == BACKEND_ID
    assert result["url"] == card_url
    assert result["title"] == "Backend Developer"
    assert result["company_name"] == "Initech"
    assert result["work_format"] == "remote"


async def test_get_vacancy_not_found_returns_none():
    """Карточка 404 либо нет в ленте и нет ссылки → None (docs/04 §5)."""
    card_url = f"{HOME_URL}/remote-jobs/initech-backend-developer"
    adapter = _adapter(
        {
            RSS_URL: RuntimeError("connection reset"),
            card_url: FetchResponse(status_code=404, text="", url=card_url),
        }
    )
    adapter.parse_listing(LISTING_HTML)
    assert await adapter.get_vacancy(BACKEND_ID) is None

    # лента здорова, но вакансии в ней нет и ссылки для карточки нет
    healthy = _adapter({RSS_URL: _rss_response()})
    assert await healthy.get_vacancy("deadbeefdeadbeefdeadbeef") is None


# --- SourceRegistry и хуки оркестратора --------------------------------------


def test_default_registry_registers_weworkremotely():
    """default_registry() содержит hh и weworkremotely; create() отдаёт экземпляр."""
    registry = default_registry()

    assert "hh" in registry.names()
    assert "weworkremotely" in registry.names()

    adapter = registry.create("weworkremotely")
    assert isinstance(adapter, WeWorkRemotelyAdapter)
    assert adapter.source_name == "weworkremotely"


def test_orchestrator_accepts_weworkremotely_source():
    """ParsingOrchestrator собирает адаптер через SourceRegistry (docs/04 §10.2)."""
    from app.modules.parsing.service import ParsingOrchestrator

    orchestrator = ParsingOrchestrator(sources=["weworkremotely"])

    assert orchestrator.adapter.source_name == "weworkremotely"
    assert "weworkremotely" in orchestrator.adapters


def test_orchestrator_hooks():
    """Хуки ParsingOrchestrator: URL выдачи, валидация, пагинация, карточки."""
    adapter = _adapter()

    # build_search_url: RSS-ленты категорий и общая лента
    assert adapter.build_search_url() == RSS_URL
    assert adapter.build_search_url(category="programming") == CATEGORY_FEED_URL
    assert adapter.build_search_url(category="Design") == (
        f"{HOME_URL}/categories/remote-design-jobs.rss"
    )
    # keywords/занятость не помещаются в URL ленты — фильтруются в search
    assert adapter.build_search_url(keywords=["python"]) == RSS_URL

    # validate_search_url: белый список доменов
    assert adapter.validate_search_url(CATEGORY_LISTING_URL) == CATEGORY_LISTING_URL
    with pytest.raises(ValueError):
        adapter.validate_search_url("https://evil.example.com/remote-jobs")
    with pytest.raises(ValueError):
        adapter.validate_search_url("ftp://weworkremotely.com/remote-jobs")

    # page_url: у ленты пагинации нет, HTML — ?page=N
    assert adapter.page_url(RSS_URL, 0) == RSS_URL
    assert adapter.page_url(RSS_URL, 3) == RSS_URL
    assert adapter.page_url(LISTING_URL, 0) == LISTING_URL
    assert adapter.page_url(LISTING_URL, 1) == f"{LISTING_URL}?page=2"

    # parse_listing: RSS (основной путь) и HTML (fallback)
    ids, has_next = adapter.parse_listing(RSS_XML)
    assert ids == [ACME_ID, GLOBEX_ID, FRONTEND_ID]
    assert has_next is False

    html_ids, html_has_next = adapter.parse_listing(LISTING_HTML)
    assert html_ids == [BACKEND_ID, SAMSARA_ID]
    assert html_has_next is True

    # build_vacancy_url: ссылка из кэша выдачи; неизвестный id → ValueError
    assert adapter.build_vacancy_url(RSS_URL, ACME_ID) == (
        "https://weworkremotely.com/remote-jobs/acme-corp-senior-python-engineer"
    )
    assert adapter.build_vacancy_url(LISTING_URL, BACKEND_ID) == (
        f"{HOME_URL}/remote-jobs/initech-backend-developer"
    )
    with pytest.raises(ValueError):
        adapter.build_vacancy_url(RSS_URL, "000000000000000000000000")


def test_extract_fields_returns_db_content_columns_only():
    """Карточка → только контент-колонки vacancies (их ждёт _ingest оркестратора)."""
    fields = _adapter().extract_fields(CARD_HTML)

    assert fields["title"] == "Backend Developer"
    assert fields["company_name"] == "Initech"
    assert fields["description_raw"] == (
        "We build APIs with Python.\nGreat opportunity for experienced developers."
    )
    assert "<b>APIs</b>" in fields["description_html"]
    assert fields["work_format"] == "remote"

    allowed = {
        "title",
        "company_name",
        "salary_from",
        "salary_to",
        "salary_currency",
        "experience",
        "employment_form",
        "work_format",
        "schedule",
        "area",
        "published_at",
        "description_raw",
        "description_html",
    }
    assert set(fields) <= allowed


def test_extract_fields_falls_back_to_page_title():
    """Без h1/hero-описания компания и роль берутся из <title> страницы."""
    html = (
        "<html><head><title>Remote Backend Developer at Initech</title></head>"
        "<body></body></html>"
    )
    fields = _adapter().extract_fields(html)

    assert fields["title"] == "Backend Developer"
    assert fields["company_name"] == "Initech"







