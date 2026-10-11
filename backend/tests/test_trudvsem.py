"""Tests for Trudvsem adapter."""

from __future__ import annotations

import json

import pytest
from app.modules.anti_ban.session import FetchResponse
from app.modules.parsing.sources import default_registry
from app.modules.parsing.sources import trudvsem as trudvsem_module
from app.modules.parsing.sources.trudvsem import (
    API_BASE,
    LISTING_URL,
    TrudvsemAdapter,
    TrudvsemAPIError,
    api_url_for,
    card_url_for,
    extract_card_fields,
    has_next_page,
    parse_api_vacancies,
    parse_html_jobs,
    region_code_for,
)

UUID = "c50d9cd8-bbe3-11f1-9a11-2f8460579df4"
HEX_ID = "c50d9cd8bbe311f19a112f8460579df4"
COMPANYCODE = "1227700702074"
CARD_URL = f"https://trudvsem.ru/vacancy/card/{COMPANYCODE}/{UUID}"

API_VACANCY = {
    "id": UUID,
    "job-name": "Razrabotchik Python",
    "company": {"companycode": COMPANYCODE, "name": "OOO Test"},
    "region": {"region_code": "6300000000000", "name": "Samarskaya oblast"},
    "salary_min": 90000,
    "salary_max": 100000,
    "currency": "rub.",
    "schedule": "Polnyi rabochii den full",
    "requirement": {"education": "Srednee", "experience": 1},
    "duty": "Razrabotka PO na Python.",
    "requirements": "Opyt raboty s FastAPI.",
    "skills": ["Python", "PostgreSQL"],
    "vac_url": CARD_URL,
    "creation-date": "2026-09-29",
    "addresses": {"address": [{"location": "Samara, shosse 32"}]},
}

API_VACANCY_OTHER = {
    "id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
    "job-name": "Dizainer",
    "company": {"companycode": "999", "name": "OOO Dizain"},
    "region": {"region_code": "7700000000000", "name": "Moskva"},
    "salary_min": 0,
    "salary_max": 0,
    "schedule": "Nepolnyi rabochii den part",
    "duty": "Dizain interfeisov.",
    "vac_url": "https://trudvsem.ru/vacancy/card/999/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
}

API_PAYLOAD = {
    "status": "200",
    "request": {"api": "v1"},
    "meta": {"total": 2},
    "results": {"vacancies": [{"vacancy": API_VACANCY}, {"vacancy": API_VACANCY_OTHER}]},
}

LISTING_HTML = f"""
<html><body>
<div class="vacancy">
  <a href="/vacancy/card/{COMPANYCODE}/{UUID}">Razrabotchik Python</a>
</div>
<div class="vacancy">
  <a href="https://trudvsem.ru/vacancy/card/999/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee">Dizainer</a>
</div>
<a rel="next" href="/vacancy/search?page=2">Next</a>
</body></html>
"""

CARD_HTML = """
<html><head>
<meta property="og:title" content="Razrabotchik Python">
<meta property="og:description" content="Razrabotka PO.">
</head><body>
<h1>Razrabotchik Python</h1>
<div class="vacancy-description"><p>Delaem servisy na Python.</p></div>
</body></html>
"""


class FakeSession:
    def __init__(self, pages: dict | None = None):
        self.pages = pages or {}
        self.requested: list[str] = []

    async def fetch(self, url: str, **_kwargs) -> FetchResponse:
        self.requested.append(url)
        result = self.pages.get(
            url, FetchResponse(status_code=404, text="", url=url)
        )
        if isinstance(result, Exception):
            raise result
        return result


def _api_response(payload: object | None = None) -> FetchResponse:
    return FetchResponse(
        status_code=200, text=json.dumps(payload or API_PAYLOAD), url=API_BASE
    )


def _adapter(pages: dict | None = None) -> TrudvsemAdapter:
    return TrudvsemAdapter(session=FakeSession(pages))


def test_normalize_maps_api_payload() -> None:
    adapter = _adapter()
    result = adapter.normalize(API_VACANCY)
    assert result["source"] == "trudvsem"
    assert result["external_id"] == HEX_ID
    assert len(result["external_id"]) == 32
    assert result["url"] == CARD_URL
    assert result["title"] == "Razrabotchik Python"
async def test_search_via_api() -> None:
    url = api_url_for(limit=100)
    adapter = _adapter({url: _api_response()})
    cards = await adapter.search({})
    assert [c["external_id"] for c in cards] == [
        HEX_ID,
        "aaaaaaaabbbbccccddddeeeeeeeeeeee",
    ]
    assert all(c["source"] == "trudvsem" for c in cards)
    assert cards[0]["url"] == CARD_URL


async def test_search_filters_region() -> None:
    url = api_url_for(limit=100)
    adapter = _adapter({url: _api_response()})
    cards = await adapter.search({"region": "Samarskaya oblast"})
    assert [c["external_id"] for c in cards] == [HEX_ID]


async def test_search_filters_employment() -> None:
    url = api_url_for(limit=100)
    adapter = _adapter({url: _api_response()})
    full = await adapter.search({"employment_type": ["full"]})
    assert [c["external_id"] for c in full] == [HEX_ID]
    part = await adapter.search({"employment_type": ["part"]})
    assert [c["external_id"] for c in part] == [
        "aaaaaaaabbbbccccddddeeeeeeeeeeee"
    ]


async def test_search_respects_limit() -> None:
    url = api_url_for(limit=100)
    adapter = _adapter({url: _api_response()})
    cards = await adapter.search({"limit": 1})
    assert len(cards) == 1


async def test_search_falls_back_to_html(monkeypatch) -> None:
    url = api_url_for(limit=100)
    monkeypatch.setattr(
        trudvsem_module, "_fetch_html", lambda *_a, **_k: LISTING_HTML
    )
    adapter = _adapter({url: RuntimeError("api down")})
    cards = await adapter.search({})
    assert [c["external_id"] for c in cards] == [
        HEX_ID,
        "aaaaaaaabbbbccccddddeeeeeeeeeeee",
    ]
    assert cards[0]["url"] == CARD_URL
    normalized = adapter.normalize(cards[0])
    assert normalized["title"] == "Razrabotchik Python"


async def test_api_non_json_raises() -> None:
    adapter = _adapter(
        {API_BASE: FetchResponse(status_code=200, text="<html></html>", url=API_BASE)}
    )
    with pytest.raises(TrudvsemAPIError):
        await adapter._fetch_api_page(API_BASE, 0)


async def test_get_vacancy_via_api() -> None:
    adapter = _adapter({API_BASE: _api_response()})
    by_id = await adapter.get_vacancy(HEX_ID)
    assert by_id is not None
    assert by_id["external_id"] == HEX_ID
    assert by_id["source"] == "trudvsem"
    by_url = await adapter.get_vacancy(CARD_URL)
    assert by_url is not None
    assert by_url["external_id"] == HEX_ID
    assert await adapter.get_vacancy("0" * 32) is None


async def test_get_vacancy_falls_back_to_card() -> None:
    pages = {
        API_BASE: RuntimeError("api down"),
        CARD_URL: FetchResponse(status_code=200, text=CARD_HTML, url=CARD_URL),
    }
    adapter = _adapter(pages)
    result = await adapter.get_vacancy(CARD_URL)
    assert result is not None
    assert result["external_id"] == HEX_ID
    assert result["title"] == "Razrabotchik Python"


def test_default_registry_registers_trudvsem() -> None:
    registry = default_registry()
    assert "trudvsem" in registry.names()
    assert "hh" in registry.names()
    adapter = registry.create("trudvsem")
    assert isinstance(adapter, TrudvsemAdapter)
    assert adapter.source_name == "trudvsem"


def test_orchestrator_hooks() -> None:
    adapter = _adapter()
    search_url = adapter.build_search_url(keywords=["python"])
    assert "opendata.trudvsem.ru" in search_url
    assert "text=python" in search_url
    region_url = adapter.build_search_url(region="Samara")
    assert "/region/6300000000000" in region_url
    assert adapter.validate_search_url(LISTING_URL) == LISTING_URL
    assert adapter.validate_search_url(API_BASE) == API_BASE
    with pytest.raises(ValueError):
        adapter.validate_search_url("https://evil.example.com/jobs")
    base = api_url_for(limit=100)
    assert adapter.page_url(base, 0) == base
    page1 = adapter.page_url(base, 1)
    assert "offset=100" in page1
    assert adapter.build_vacancy_url(base, HEX_ID) == base
    adapter.parse_listing(json.dumps(API_PAYLOAD))
    assert adapter.build_vacancy_url(base, HEX_ID) == CARD_URL
    assert card_url_for(COMPANYCODE, UUID) == CARD_URL
    jobs, total = parse_api_vacancies(json.dumps(API_PAYLOAD))
    assert total == 2
    assert len(jobs) == 2
    html_jobs = parse_html_jobs(LISTING_HTML)
    assert [j["external_id"] for j in html_jobs] == [
        HEX_ID,
        "aaaaaaaabbbbccccddddeeeeeeeeeeee",
    ]
    assert has_next_page(LISTING_HTML) is True
    assert has_next_page("<html></html>") is False
    fields = extract_card_fields(CARD_HTML)
    assert fields["title"] == "Razrabotchik Python"
    assert "Python" in (fields.get("description_raw") or "")


def test_normalize_zero_salary() -> None:
    result = _adapter().normalize(API_VACANCY_OTHER)
    assert result["salary_from"] is None
    assert result["salary_to"] is None
    assert result["salary_currency"] is None


def test_region_code_for() -> None:
    assert region_code_for("63") == "6300000000000"
    assert region_code_for("Moskva") == "7700000000000"
    assert region_code_for("Samara") == "6300000000000"
    assert region_code_for(None) is None

