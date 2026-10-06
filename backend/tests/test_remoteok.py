"""Тесты адаптера RemoteOK: normalize, search (API-мок) и HTML-fallback.

Покрывают docs/04_PARSING_RULES.md §10 (адаптер источника, SourceRegistry):

- ``normalize`` — маппинг payload API в каноническую схему §7;
- ``search`` — фильтры по ключевым словам и тегам, пропуск legal-элемента;
- fallback — при отказе/троттлинге API разбор HTML через curl_cffi;
- регистрация ``RemoteOKAdapter`` в ``default_registry()``;
- хуки оркестратора (build_search_url/parse_listing/extract_fields).

Сеть не используется: API подменяется FakeSession (FetchResponse), а
``_fetch_html`` (curl_cffi) — monkeypatch'ем, поэтому тесты проходят
без внешних HTTP-запросов.
"""

from __future__ import annotations

import json

import pytest
from app.modules.anti_ban.session import FetchResponse
from app.modules.parsing.sources import default_registry
from app.modules.parsing.sources import remoteok as remoteok_module
from app.modules.parsing.sources.remoteok import (
    API_URL,
    HOME_URL,
    LISTING_URL,
    RemoteOKAdapter,
    RemoteOKAPIError,
)

# --- фикстуры payload API ----------------------------------------------------

LEGAL_NOTICE = {
    "last_updated": 1791205202,
    "legal": "API Terms of Service: Please link back to Remote OK...",
}

API_JOB_PYTHON = {
    "slug": "senior-python-engineer-acme-111111",
    "id": "111111",
    "epoch": 1791129603,
    "date": "2026-10-04T16:00:03+00:00",
    "company": "Acme",
    "position": "Senior Python Engineer",
    "tags": ["python", "django", "fastapi"],
    "description": "<p>Build APIs with <b>Python</b>.</p>",
    "location": "Worldwide",
    "salary_min": 60000,
    "salary_max": 90000,
    "url": "https://remoteOK.com/remote-jobs/senior-python-engineer-acme-111111",
}

API_JOB_DESIGNER = {
    "slug": "ux-designer-initech-222222",
    "id": "222222",
    "date": "2026-10-01T09:00:00+00:00",
    "company": "Initech",
    "position": "UX Designer",
    "tags": ["design", "figma"],
    "description": "<p>Design delightful interfaces.</p>",
    "location": "Europe",
    "salary_min": 0,
    "salary_max": 0,
    "url": "https://remoteok.com/remote-jobs/ux-designer-initech-222222",
}

API_JOB_DEVOPS = {
    "slug": "devops-engineer-umbrella-333333",
    "id": "333333",
    "date": "2026-09-28T12:00:00+00:00",
    "company": "Umbrella",
    "position": "DevOps Engineer",
    "tags": ["devops", "aws"],
    "description": "<p>Run Kubernetes clusters.</p>",
    "location": "",
    "salary_min": None,
    "salary_max": None,
    # url намеренно отсутствует — normalize должен построить ссылку по id.
}

API_PAYLOAD = [LEGAL_NOTICE, API_JOB_PYTHON, API_JOB_DESIGNER, API_JOB_DEVOPS]

# --- фикстуры HTML (fallback и карточка) -------------------------------------

LISTING_HTML = """
<html><body>
<nav><a href="/remote-jobs">Remote jobs</a></nav>
<div class="job" data-id="222222" data-company="Globex" data-location="Europe"
     data-tags="python,go" data-salary="$80,000 - $110,000">
  <a class="position" href="https://remoteok.com/l/222222">Backend Developer</a>
</div>
<div class="job" data-id="333333" data-company="Initech" data-location="Worldwide"
     data-tags="design,figma">
  <a href="/remote-jobs/ux-designer-initech-333333">UX Designer</a>
</div>
<a rel="next" href="/remote-jobs?page=2">Next</a>
</body></html>
"""

CARD_HTML = """
<html><head>
<title>Backend Developer @ Globex | Remote OK</title>
<meta property="og:title" content="Backend Developer @ Globex | Remote OK">
<meta name="description" content="Build backends for Globex.">
</head><body>
<div class="job" data-company="Globex" data-location="Europe"
     data-tags="python,go" data-salary="$80,000 - $110,000">
  <h1>Backend Developer</h1>
  <div class="description"><p>We build APIs with <b>Python</b>.</p></div>
</div>
</body></html>
"""


class FakeSession:
    """AntiBanSession с подменённым fetch: без сети, прогрева и пауз.

    Значением в ``pages`` может быть FetchResponse либо исключение —
    тогда fetch его выбросит (сценарий «API упал»).
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


def _api_response(payload: object | None = None, status: int = 200) -> FetchResponse:
    body = API_PAYLOAD if payload is None else payload
    return FetchResponse(status_code=status, text=json.dumps(body), url=API_URL)


def _adapter(pages: dict | None = None) -> RemoteOKAdapter:
    return RemoteOKAdapter(session=FakeSession(pages))


# --- normalize ---------------------------------------------------------------


def test_normalize_maps_api_payload_to_canonical_schema():
    """Payload API → каноническая схема docs/04 §7 + tags/remote."""
    adapter = _adapter()
    result = adapter.normalize(API_JOB_PYTHON)

    assert result["source"] == "remoteok"
    assert result["external_id"] == "111111"
    # хост remoteOK.com нормализуется к каноническому регистру
    assert result["url"] == (
        "https://remoteok.com/remote-jobs/senior-python-engineer-acme-111111"
    )
    assert result["title"] == "Senior Python Engineer"
    assert result["company_name"] == "Acme"
    assert result["salary_from"] == 60000
    assert result["salary_to"] == 90000
    assert result["salary_currency"] == "USD"
    assert result["area"] == "Worldwide"
    assert result["published_at"] == "2026-10-04T16:00:03+00:00"
    assert result["description_raw"] == "Build APIs with Python."
    assert result["description_html"] == "<p>Build APIs with <b>Python</b>.</p>"
    assert result["work_format"] == "remote"
    # поля задачи
    assert result["tags"] == ["python", "django", "fastapi"]
    assert result["remote"] is True
    # алиасы
    assert result["company"] == "Acme"
    assert result["currency"] == "USD"
    assert result["description"] == "Build APIs with Python."
    # неизвестные для RemoteOK поля канонической схемы присутствуют
    assert result["experience"] is None
    assert result["employment_form"] is None
    assert result["schedule"] is None


def test_normalize_zero_salary_means_no_data():
    """salary_min/max = 0 → зарплата и валюта не заполняются."""
    result = _adapter().normalize(API_JOB_DESIGNER)
    assert result["salary_from"] is None
    assert result["salary_to"] is None
    assert result["salary_currency"] is None
    assert result["remote"] is True


def test_normalize_builds_url_from_external_id():
    """Без url карточка получает каноническую ссылку https://remoteok.com/l/<id>."""
    result = _adapter().normalize(API_JOB_DEVOPS)
    assert result["external_id"] == "333333"
    assert result["url"] == "https://remoteok.com/l/333333"
    assert result["salary_currency"] is None


# --- search через API --------------------------------------------------------


async def test_search_skips_legal_and_returns_cards():
    """Первый элемент (legal) пропускается; карточки содержат source/external_id/url."""
    adapter = _adapter({API_URL: _api_response()})
    cards = await adapter.search({})

    assert [card["external_id"] for card in cards] == ["111111", "222222", "333333"]
    for card in cards:
        assert card["source"] == "remoteok"
        assert card["url"].startswith("https://remoteok.com/")
    # legal-уведомление не превратилось в вакансию
    assert all("legal" not in card for card in cards)


async def test_search_filters_by_keywords():
    """keywords — AND по названию/компании/локации/тегам/описанию."""
    adapter = _adapter({API_URL: _api_response()})

    python_only = await adapter.search({"keywords": ["python"]})
    assert [card["external_id"] for card in python_only] == ["111111"]

    engineers = await adapter.search({"keywords": ["engineer"]})
    assert [card["external_id"] for card in engineers] == ["111111", "333333"]

    # несколько слов через запятую — нормализуются в список
    both = await adapter.search({"keywords": "senior, acme"})
    assert [card["external_id"] for card in both] == ["111111"]


async def test_search_filters_by_tags():
    """tags — OR по тегам вакансии."""
    adapter = _adapter({API_URL: _api_response()})

    devops = await adapter.search({"tags": ["devops"]})
    assert [card["external_id"] for card in devops] == ["333333"]

    design_or_python = await adapter.search({"tags": ["design", "python"]})
    assert [card["external_id"] for card in design_or_python] == [
        "111111",
        "222222",
    ]

    none = await adapter.search({"tags": ["blockchain"]})
    assert none == []


async def test_search_respects_limit():
    """max_results ограничивает число карточек."""
    adapter = _adapter({API_URL: _api_response()})
    cards = await adapter.search({"max_results": 2})
    assert len(cards) == 2


async def test_api_non_json_raises_remoteok_api_error():
    """Не-JSON ответ API → RemoteOKAPIError (сигнал включить HTML-fallback)."""
    adapter = _adapter(
        {API_URL: FetchResponse(status_code=200, text="<html>captcha</html>", url=API_URL)}
    )
    with pytest.raises(RemoteOKAPIError):
        await adapter._fetch_api_jobs()


# --- HTML-fallback (curl_cffi) -----------------------------------------------


async def test_search_falls_back_to_html_when_api_fails(monkeypatch):
    """Сбой API → лёгкий HTML-парсинг главной remoteok.com (без сети в тестах)."""
    fetched: list[str] = []

    def fake_fetch_html(url: str, **_kwargs) -> str:
        fetched.append(url)
        return LISTING_HTML

    monkeypatch.setattr(remoteok_module, "_fetch_html", fake_fetch_html)
    adapter = _adapter({API_URL: RuntimeError("connection reset")})

    cards = await adapter.search({})

    # API запрашивался через сессию, HTML — через curl_cffi-функцию модуля
    assert adapter.http.requested == [API_URL]
    assert fetched == [HOME_URL]
    assert [card["external_id"] for card in cards] == ["222222", "333333"]
    for card in cards:
        assert card["source"] == "remoteok"
        assert card["url"].startswith("https://remoteok.com/")

    # данные HTML-карточки нормализуются в каноническую схему
    normalized = adapter.normalize(cards[0])
    assert normalized["title"] == "Backend Developer"
    assert normalized["company_name"] == "Globex"
    assert normalized["area"] == "Europe"
    assert normalized["salary_from"] == 80000
    assert normalized["salary_to"] == 110000
    assert normalized["salary_currency"] == "USD"
    assert normalized["tags"] == ["python", "go"]
    assert normalized["remote"] is True

    # фильтр по ключевому слову работает и по HTML-выдаче
    backend = await adapter.search({"keywords": ["backend"]})
    assert [card["external_id"] for card in backend] == ["222222"]


async def test_search_falls_back_to_html_on_throttled_api(monkeypatch):
    """HTTP 429 от API (троттлинг) → тот же HTML-fallback."""
    monkeypatch.setattr(
        remoteok_module, "_fetch_html", lambda *_args, **_kwargs: LISTING_HTML
    )
    adapter = _adapter({API_URL: FetchResponse(status_code=429, text="", url=API_URL)})

    cards = await adapter.search({})

    assert [card["external_id"] for card in cards] == ["222222", "333333"]


# --- get_vacancy -------------------------------------------------------------


async def test_get_vacancy_via_api():
    """Основной путь: карточка из API по id и по прямой ссылке; 404 → None."""
    adapter = _adapter({API_URL: _api_response()})

    by_id = await adapter.get_vacancy("111111")
    assert by_id is not None
    assert by_id["title"] == "Senior Python Engineer"
    assert by_id["source"] == "remoteok"

    by_url = await adapter.get_vacancy(API_JOB_PYTHON["url"])
    assert by_url is not None
    assert by_url["external_id"] == "111111"

    assert await adapter.get_vacancy("999999") is None


async def test_get_vacancy_falls_back_to_card_html():
    """API упал → карточка запрашивается по ссылке и разбирается из HTML."""
    pages = {
        API_URL: RuntimeError("api down"),
        "https://remoteok.com/l/222222": FetchResponse(
            status_code=200, text=CARD_HTML, url="https://remoteok.com/l/222222"
        ),
    }
    adapter = _adapter(pages)

    result = await adapter.get_vacancy("222222")

    assert result is not None
    assert result["source"] == "remoteok"
    assert result["external_id"] == "222222"
    assert result["title"] == "Backend Developer @ Globex"
    assert result["company_name"] == "Globex"
    assert result["salary_from"] == 80000
    assert result["description_raw"] == "We build APIs with Python."
    assert result["remote"] is True


async def test_get_vacancy_card_not_found_returns_none():
    """API упал, карточка отдаёт 404 → None (docs/04 §5 not_found)."""
    adapter = _adapter({API_URL: RuntimeError("api down")})
    assert await adapter.get_vacancy("444444") is None


# --- SourceRegistry и хуки оркестратора --------------------------------------


def test_default_registry_registers_remoteok():
    """default_registry() содержит hh и remoteok; create() отдаёт экземпляр."""
    registry = default_registry()

    assert "hh" in registry.names()
    assert "remoteok" in registry.names()

    adapter = registry.create("remoteok")
    assert isinstance(adapter, RemoteOKAdapter)
    assert adapter.source_name == "remoteok"


def test_orchestrator_hooks():
    """Хуки ParsingOrchestrator: URL выдачи, валидация, пагинация, карточки."""
    adapter = _adapter()

    assert adapter.build_search_url(keywords=["python"]) == LISTING_URL
    assert adapter.build_search_url(page=1) == f"{LISTING_URL}?page=2"
    assert adapter.validate_search_url(LISTING_URL) == LISTING_URL
    with pytest.raises(ValueError):
        adapter.validate_search_url("https://evil.example.com/remote-jobs")

    assert adapter.page_url(LISTING_URL, 0) == LISTING_URL
    assert adapter.page_url(LISTING_URL, 1) == f"{LISTING_URL}?page=2"

    ids, has_next = adapter.parse_listing(LISTING_HTML)
    assert ids == ["222222", "333333"]
    assert has_next is True

    assert adapter.build_vacancy_url(LISTING_URL, "111111") == (
        "https://remoteok.com/l/111111"
    )


def test_extract_fields_from_card_html():
    """extract_fields разбирает карточку HTML (шапка + описание + data-атрибуты)."""
    fields = _adapter().extract_fields(CARD_HTML)

    assert fields["title"] == "Backend Developer @ Globex"
    assert fields["company"] == "Globex"
    assert fields["location"] == "Europe"
    assert fields["description_raw"] == "We build APIs with Python."
    assert fields["tags"] == ["python", "go"]
    assert fields["salary_from"] == 80000
    assert fields["salary_to"] == 110000


