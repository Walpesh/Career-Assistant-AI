"""Тесты адаптера Lever: normalize, search (API-мок) и HTML-fallback.

Покрывают docs/04_PARSING_RULES.md §10 (адаптер источника, SourceRegistry):

- ``normalize`` — маппинг payload Postings API в каноническую схему §7;
- ``search`` — основной путь через ``api.lever.co`` и клиентский фильтр
  ``keywords``;
- fallback — при отказе/непригодности API разбор HTML-выдачи
  ``jobs.lever.co/{site}``;
- ``get_vacancy`` — одна вакансия через API и через HTML-карточку;
- регистрация ``LeverAdapter`` в ``default_registry()``;
- хуки оркестратора (build_search_url/validate_search_url/page_url/
  parse_listing/build_vacancy_url/extract_fields).

Сеть не используется: API подменяется FakeSession (FetchResponse), поэтому
тесты проходят без внешних HTTP-запросов.
"""

from __future__ import annotations

import json
from unittest.mock import Mock

import pytest
from app.modules.anti_ban import AntiBanSession
from app.modules.anti_ban.session import FetchResponse
from app.modules.parsing.sources import default_registry
from app.modules.parsing.sources.lever import (
    API_BASE,
    LISTING_BASE,
    PRESET_COMPANY_NAMES,
    PRESET_SITES,
    LeverAdapter,
    LeverAPIError,
    api_posting_url_for,
    api_url_for,
    company_name_for,
    extract_card_fields,
    has_next_page,
    job_url_for,
    listing_url_for,
    normalize_site,
    parse_api_postings,
    parse_html_postings,
)

from app.modules.parsing.sources.registry import UnknownSourceError

#: Доска тестовой компании.
SITE = "rover"
API_URL = api_url_for(SITE)
LISTING_URL = listing_url_for(SITE)
BACKEND_ID = "c8302568-bf57-4298-a45b-92d3313a74b8"
DESIGNER_ID = "d9413679-c081-4123-9abc-34e4425b1050"
BACKEND_URL = job_url_for(SITE, BACKEND_ID)
DESIGNER_URL = job_url_for(SITE, DESIGNER_ID)
BACKEND_DETAIL_URL = api_posting_url_for(SITE, BACKEND_ID)
DESIGNER_DETAIL_URL = api_posting_url_for(SITE, DESIGNER_ID)

# --- фикстуры payload Postings API --------------------------------------------

API_POSTING_BACKEND = {
    "id": BACKEND_ID,
    "text": "Senior Backend Engineer",
    "hostedUrl": BACKEND_URL,
    "applyUrl": f"{BACKEND_URL}/apply",
    "categories": {
        "location": "Remote",
        "team": "Engineering",
        "commitment": "Full-time",
        "department": "Engineering",
    },
    "description": "<p>We build <strong>Python</strong> services.</p>",
    "descriptionPlain": "We build Python services.",
    "lists": [{"text": "About the role", "content": "<p>Ship production code.</p>"}],
    "country": "United States",
    "workplaceType": "remote",
    "createdAt": 1727865600000,
    "salaryRange": {"min": 150000, "max": 190000, "currency": "USD"},
}

API_POSTING_DESIGNER = {
    "id": DESIGNER_ID,
    "text": "Product Designer",
    "hostedUrl": DESIGNER_URL,
    "categories": {
        "location": "San Francisco, CA",
        "team": "Design",
        "commitment": "Full-time",
    },
    "description": "<p>Design delightful interfaces.</p>",
    "descriptionPlain": "Design delightful interfaces.",
    "lists": [],
    "country": "United States",
    "workplaceType": "on-site",
    "createdAt": 1727740800000,
}

API_LIST = [API_POSTING_BACKEND, API_POSTING_DESIGNER]

# --- HTML-выдача (jobs.lever.co/rover) и HTML-карточка --------------------------

LISTING_HTML = f"""
<html><head><title>Jobs at Rover</title></head><body>
<div class="large-category-header">Engineering</div>
<div class="postings-group">
  <div class="posting" data-location="Remote">
    <a class="posting-title" href="{BACKEND_URL}">
      <h5>Senior Backend Engineer</h5>
      <span class="sort-by-location">Remote</span>
      <span class="sort-by-commitment">Full-time</span>
    </a>
  </div>
  <div class="large-category-header">Design</div>
  <div class="posting" data-location="San Francisco, CA">
    <a class="posting-title" href="{DESIGNER_URL}">
      <h5>Product Designer</h5>
      <span class="sort-by-location">San Francisco, CA</span>
      <span class="sort-by-commitment">Full-time</span>
    </a>
  </div>
</div>
</body></html>
"""

CARD_HTML = """
<html><head>
<title>Rover - Senior Backend Engineer</title>
<meta property="og:site_name" content="Rover" />
</head><body>
<div class="posting-headline"><h2>Senior Backend Engineer</h2></div>
<div class="sort-by-location posting-category">Remote</div>
<div class="sort-by-team posting-category">Engineering</div>
<div class="sort-by-commitment posting-category">Full-time</div>
<div data-qa="job-description"><p>We build <strong>Python</strong> services.</p></div>
</body></html>
"""

# --- вспомогательные объекты ---------------------------------------------------

def _adapter(pages: dict | None = None, *, site: str | None = None):
    """Замыкание: адаптер с мок-сессией, не тянущий AntiBanSession."""
    session = Mock(spec=AntiBanSession)
    pages = dict(pages or {})

    async def mock_fetch(url, **kwargs):
        if url in pages:
            result = pages[url]
            if isinstance(result, Exception):
                raise result
            return result
        if url.startswith(API_BASE):
            return _api_response()
        return _response(404, "", url)

    session.fetch = mock_fetch
    return LeverAdapter(session=session, site=site or SITE)


# --- вспомогательные объекты (продолжение) -------------------------------------

def _response(status_code: int = 200, text: str = "", url: str = "") -> FetchResponse:
    """Фиктивный ответ сессии."""
    return FetchResponse(status_code=status_code, text=text, url=url)


def _api_response(payload=None, url: str = API_URL) -> FetchResponse:
    """Успешный ответ Postings API."""
    return _response(200, json.dumps(payload if payload is not None else API_LIST), url)


def _api_error(status_code: int = 403, url: str = API_URL) -> FetchResponse:
    """Ответ сервера (403/5xx) вместо JSON."""
    return _response(status_code, f"Lever error {status_code}: {url}", url)


# --- URL-билдеры ---------------------------------------------------------------

def test_api_url_for_builds_api_listings_url():
    assert api_url_for(SITE) == f"{API_BASE}/{SITE}?mode=json"
    assert api_url_for(SITE, "eu").startswith("https://api.eu.lever.co/")


def test_api_posting_url_for_builds_detail_url():
    assert api_posting_url_for(SITE, BACKEND_ID) == f"{API_BASE}/{SITE}/{BACKEND_ID}"
    assert api_posting_url_for(SITE, BACKEND_ID, "eu").startswith("https://api.eu.lever.co/")


def test_listing_and_job_url_for():
    assert listing_url_for(SITE) == f"{LISTING_BASE}/{SITE}"
    assert job_url_for(SITE, BACKEND_ID) == f"{LISTING_BASE}/{SITE}/{BACKEND_ID}"
    assert listing_url_for(SITE, "eu").startswith("https://jobs.eu.lever.co/")


# --- компания / нормализация слага ----------------------------------------------

def test_company_name_for():
    assert company_name_for(SITE) == "Rover"
    assert company_name_for("rover") == "Rover"
    assert company_name_for("ramp") == "Ramp"
    assert company_name_for("no-such-company") is None


def test_normalize_site_accepts_slug_and_rejects_invalid():
    assert normalize_site("rover") == "rover"
    assert normalize_site("  rover  ") == "rover"
    assert normalize_site("rover/") == "rover"
    assert normalize_site("rover-123") == "rover-123"
    assert normalize_site("Rover") == "Rover"
    assert normalize_site("") is None
    assert normalize_site(None) is None
    with pytest.raises(ValueError):
        normalize_site("https://jobs.lever.co/rover")
    with pytest.raises(ValueError):
        normalize_site("rover/123")


def test_preset_sites_and_company_names():
    assert list(PRESET_SITES) == ["rover", "ramp", "coupa", "aircall", "zoox"]
    assert PRESET_COMPANY_NAMES["zoox"] == "Zoox"


# --- нормализация (maps Postings API в схему §7) -------------------------------

def test_normalize_maps_fields():
    adapter = _adapter()
    raw = {
        "id": BACKEND_ID,
        "hostedUrl": BACKEND_URL,
        "text": "Senior Backend Engineer",
        "categories": {
            "location": "Remote",
            "team": "Engineering",
            "commitment": "Full-time",
            "department": "Engineering",
        },
        "workplaceType": "remote",
        "createdAt": 1727865600000,
        "salaryRange": {"min": 150000, "max": 190000, "currency": "USD"},
        "description": "<p>We build <strong>Python</strong> services.</p>",
        "descriptionPlain": "We build Python services.",
        "company_name": "Rover",
        "employment_form": "Full-time",
        "experience": "Senior",
        "schedule": "Flexible",
        "area": "Remote",
        "published_at": "2024-10-01T00:00:00+00:00",
        "description_html": "<p>We build <strong>Python</strong> services.</p>",
    }
    normalized = adapter.normalize(raw)

    assert normalized["source"] == "lever"
    assert normalized["external_id"] == BACKEND_ID
    assert normalized["url"] == BACKEND_URL
    assert normalized["title"] == "Senior Backend Engineer"
    assert normalized["company_name"] == "Rover"
    assert normalized["salary_from"] == 150000
    assert normalized["salary_to"] == 190000
    assert normalized["salary_currency"] == "USD"
    assert normalized["experience"] == "Senior"
    assert normalized["employment_form"] == "Full-time"
    assert normalized["work_format"] == "remote"
    assert normalized["schedule"] == "Flexible"
    assert normalized["area"] == "Remote"
    assert normalized["published_at"] == "2024-10-01T00:00:00+00:00"
    assert normalized["description_raw"] == "We build Python services."
    assert normalized["description_html"] == "<p>We build <strong>Python</strong> services.</p>"


def test_normalize_salary_from_to_override_salary_range():
    adapter = _adapter()
    raw = {
        "id": DESIGNER_ID,
        "hostedUrl": DESIGNER_URL,
        "text": "Product Designer",
        "salary_range": {"min": 100000, "max": 140000, "currency": "USD"},
        "salary_from": 90000,
        "salary_to": 130000,
        "salary_currency": "EUR",
        "workplaceType": "hybrid",
    }
    normalized = adapter.normalize(raw)

    assert normalized["salary_from"] == 90000
    assert normalized["salary_to"] == 130000
    assert normalized["salary_currency"] == "EUR"
    assert normalized["work_format"] == "hybrid"


def test_normalize_uses_categories_when_field_missing():
    adapter = _adapter()
    raw = {
        "id": BACKEND_ID,
        "hostedUrl": BACKEND_URL,
        "text": "Senior Backend Engineer",
        "workplaceType": "on-site",
        "createdAt": 1727740800000,
        "categories": {"location": "San Francisco, CA", "commitment": "Full-time"},
    }
    normalized = adapter.normalize(raw)

    assert normalized["area"] == "San Francisco, CA"
    assert normalized["employment_form"] == "Full-time"
    assert normalized["work_format"] == "onsite"


def test_normalize_missing_title_is_none():
    adapter = _adapter()
    normalized = adapter.normalize({"id": BACKEND_ID, "url": BACKEND_URL})
    assert normalized["source"] == "lever"
    assert normalized["external_id"] == BACKEND_ID
    assert normalized["title"] is None
    assert normalized["company_name"] == "Rover"


def test_normalize_missing_external_id_raises():
    adapter = _adapter()
    with pytest.raises(ValueError, match="Vacancy missing external_id"):
        adapter.normalize({"url": BACKEND_URL})


def test_normalize_published_at_from_millis():
    adapter = _adapter()
    raw = {
        "id": BACKEND_ID,
        "hostedUrl": BACKEND_URL,
        "text": "Senior Backend Engineer",
        "createdAt": 1727740800000,
    }
    normalized = adapter.normalize(raw)
    assert normalized["published_at"] == "2024-10-01T00:00:00+00:00"


# --- парсеры -----------------------------------------------------------------

def test_parse_api_postings_returns_raw_list():
    postings = parse_api_postings(json.dumps(API_LIST))
    assert [p["id"] for p in postings] == [BACKEND_ID, DESIGNER_ID]


def test_parse_api_postings_single_object_and_empty():
    with pytest.raises(ValueError):
        parse_api_postings(json.dumps(dict(API_POSTING_BACKEND)))
    assert parse_api_postings("[]") == []


def test_parse_api_postings_rejects_garbage():
    with pytest.raises(ValueError):
        parse_api_postings("")
    with pytest.raises(ValueError):
        parse_api_postings(json.dumps({}))


def test_parse_html_postings_extracts_ids_and_urls():
    postings = parse_html_postings(LISTING_HTML)

    assert [p["external_id"] for p in postings] == [BACKEND_ID, DESIGNER_ID]
    assert postings[0]["url"] == BACKEND_URL
    assert postings[0]["title"] == "Senior Backend Engineer"
    assert postings[0]["location"] == "Remote"
    assert postings[0]["team"] == "Engineering"
    assert postings[0]["commitment"] == "Full-time"
    assert postings[1]["location"] == "San Francisco, CA"
    assert postings[1]["team"] == "Design"
    assert postings[1]["commitment"] == "Full-time"


def test_has_next_page_single_page_listing():
    assert has_next_page(LISTING_HTML) is False


# --- поиск (search) ------------------------------------------------------------

async def test_search_success_api():
    adapter = _adapter(pages={API_URL: _api_response(API_LIST)})
    results = await adapter.search({"site": SITE})

    assert [r["external_id"] for r in results] == [BACKEND_ID, DESIGNER_ID]
    assert results[0]["source"] == "lever"
    assert results[0]["url"] == BACKEND_URL


async def test_search_with_keywords_filters_results():
    adapter = _adapter(pages={API_URL: _api_response(API_LIST)})
    results = await adapter.search({"site": SITE, "keywords": "Python"})

    assert [r["external_id"] for r in results] == [BACKEND_ID]


async def test_search_empty_site_returns_empty_list():
    adapter = _adapter(pages={api_url_for("nonexistent"): _api_error(403)})
    results = await adapter.search({"site": "nonexistent"})

    assert results == []


async def test_search_fallback_to_html_on_failed_api():
    listing = listing_url_for(SITE)
    adapter = _adapter(pages={
        API_URL: _api_error(403),
        listing: _response(200, LISTING_HTML),
    })
    results = await adapter.search({"site": SITE})

    assert [r["external_id"] for r in results] == [BACKEND_ID, DESIGNER_ID]


async def test_search_presets_and_default():
    adapter = _adapter(pages={API_URL: _api_response(API_LIST)})

    results = await adapter.search({"site": PRESET_SITES[0]})
    assert [r["external_id"] for r in results] == [BACKEND_ID, DESIGNER_ID]

    results = await adapter.search({})
    assert [r["external_id"] for r in results] == [BACKEND_ID, DESIGNER_ID]


def test_extract_card_fields_reads_headline_and_description():
    """extract_card_fields читает заголовок и описание HTML-карточки."""
    fields = extract_card_fields(CARD_HTML)

    assert fields["title"] == "Senior Backend Engineer"
    assert fields["company_name"] == "Rover"
    assert fields["area"] == "Remote"
    assert "We build" in (fields["description_raw"] or "")
    assert "Python" in (fields["description_html"] or "")


def test_extract_card_fields_empty_without_title():
    """extract_card_fields без заголовка — пустые поля (карточка битая)."""
    assert extract_card_fields("<html><body>oops</body></html>") == {}


# --- вакансия (get_vacancy) ----------------------------------------------------

async def test_get_vacancy_api_success():
    adapter = _adapter(pages={BACKEND_DETAIL_URL: _api_response(API_POSTING_BACKEND)})
    result = await adapter.get_vacancy(BACKEND_ID)

    assert result["source"] == "lever"
    assert result["external_id"] == BACKEND_ID
    assert result["url"] == BACKEND_URL
    assert result["text"] == "Senior Backend Engineer"
    assert result["categories"]["location"] == "Remote"
    assert result["salaryRange"]["currency"] == "USD"
    assert result["workplaceType"] == "remote"


async def test_get_vacancy_html_fallback_on_api_fail():
    adapter = _adapter(pages={
        BACKEND_DETAIL_URL: _api_error(403, BACKEND_DETAIL_URL),
        BACKEND_URL: _response(200, CARD_HTML),
    })
    result = await adapter.get_vacancy(BACKEND_ID)

    assert result["source"] == "lever"
    assert result["external_id"] == BACKEND_ID
    assert result["url"] == BACKEND_URL
    assert result["title"] == "Senior Backend Engineer"
    assert result["company_name"] == "Rover"
    assert result["area"] == "Remote"
    assert result["employment_form"] == "Full-time"
    assert "We build" in (result["description_raw"] or "")
    assert "Python" in (result["description_html"] or "")


async def test_get_vacancy_not_found_returns_none():
    adapter = _adapter(pages={BACKEND_DETAIL_URL: _api_error(404)})
    assert await adapter.get_vacancy(BACKEND_ID) is None


async def test_get_vacancy_html_without_title_returns_none():
    adapter = _adapter(pages={
        BACKEND_DETAIL_URL: _api_error(403, BACKEND_DETAIL_URL),
        BACKEND_URL: _response(200, "<html><body></body></html>"),
    })
    assert await adapter.get_vacancy(BACKEND_ID) is None


# --- регистрация ---------------------------------------------------------------

def test_default_registry_includes_lever():
    registry = default_registry()
    assert "lever" in registry
    assert "lever" in registry.names()
    assert registry.get("lever").source_name == "lever"


def test_registry_get_adapter_lever():
    adapter = default_registry().create("lever", site=SITE)
    assert isinstance(adapter, LeverAdapter)
    assert adapter.site == SITE


async def test_unknown_source_raises():
    with pytest.raises(UnknownSourceError):
        default_registry().create("not-a-real-source")


# --- хуки оркестратора ----------------------------------------------------------

def test_build_search_url():
    adapter = _adapter()
    assert adapter.build_search_url() == api_url_for(SITE)
    assert adapter.build_search_url(company="rover") == api_url_for(SITE)


def test_validate_search_url():
    adapter = _adapter()
    assert adapter.validate_search_url(api_url_for(SITE)) == api_url_for(SITE)
    with pytest.raises(ValueError):
        adapter.validate_search_url("https://evil.example.com/")


def test_page_url():
    adapter = _adapter()
    assert adapter.page_url(api_url_for(SITE), 1) == api_url_for(SITE)


def test_parse_listing_handles_api_and_html():
    adapter = _adapter()

    vacancy_ids, has_next = adapter.parse_listing(json.dumps(API_LIST))
    assert vacancy_ids == [BACKEND_ID, DESIGNER_ID]
    assert has_next is False

    vacancy_ids, has_next = adapter.parse_listing(LISTING_HTML)
    assert vacancy_ids == [BACKEND_ID, DESIGNER_ID]
    assert has_next is False


def test_build_vacancy_url():
    adapter = _adapter()
    assert adapter.build_vacancy_url(api_url_for(SITE), BACKEND_ID) == job_url_for(
        SITE, BACKEND_ID
    )


async def test_extract_fields():
    adapter = _adapter()
    fields = adapter.extract_fields(CARD_HTML)

    assert fields["title"] == "Senior Backend Engineer"
    assert fields["company_name"] == "Rover"
    assert fields["area"] == "Remote"
    assert fields["employment_form"] == "Full-time"




