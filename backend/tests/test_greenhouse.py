"""Тесты адаптера Greenhouse: normalize, search (API-мок) и HTML-fallback.

Покрывают docs/04_PARSING_RULES.md §10 (адаптер источника, SourceRegistry):

- ``normalize`` — маппинг payload Job Board API в каноническую схему §7;
- ``search`` — основной путь через ``boards-api.greenhouse.io`` и
  клиентский фильтр ``keywords``;
- fallback — при отказе/непригодности API разбор HTML-выдачи
  ``boards.greenhouse.io/{board_token}``;
- ``get_vacancy`` — одна вакансия через API и через HTML-карточку;
- регистрация ``GreenhouseAdapter`` в ``default_registry()``;
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
from app.modules.parsing.sources.greenhouse import (
    API_BASE,
    JOB_BASE,
    LISTING_BASE,
    PRESET_BOARD_TOKENS,
    PRESET_COMPANY_NAMES,
    GreenhouseAdapter,
    GreenhouseAPIError,
    api_url_for,
    extract_card_fields,
    has_next_page,
    job_url_for,
    listing_url_for,
    normalize_board_token,
    parse_api_jobs,
    parse_html_jobs,
)

#: Доска тестовой компании.
TOKEN = "gitlab"
API_URL = api_url_for(TOKEN)
LISTING_URL = listing_url_for(TOKEN)
CARD_URL = job_url_for(TOKEN, "8860302002")
SINGLE_JOB_URL = f"{API_BASE}/{TOKEN}/jobs/8860302002?content=true"

# --- фикстуры payload API ----------------------------------------------------

API_JOB_PYTHON = {
    "id": 8860302002,
    "title": "Senior Python Engineer",
    "absolute_url": "https://job-boards.greenhouse.io/gitlab/jobs/8860302002",
    "location": {"name": "Remote, Europe"},
    "updated_at": "2026-10-02T11:31:50-04:00",
    "first_published": "2026-09-30T09:00:00-04:00",
    "company_name": "GitLab",
    "content": "<p>We build <strong>Python</strong> services.</p>",
}

API_JOB_DESIGNER = {
    "id": 8785825002,
    "title": "Product Designer",
    "absolute_url": "https://job-boards.greenhouse.io/gitlab/jobs/8785825002",
    "location": {"name": "New York, NY"},
    "updated_at": "2026-09-29T10:36:33-04:00",
    "company_name": "GitLab",
    "content": "<p>Design delightful interfaces.</p>",
}

API_PAYLOAD = {"jobs": [API_JOB_PYTHON, API_JOB_DESIGNER]}


# --- фикстуры HTML (fallback и карточка) -------------------------------------

LISTING_HTML = """
<html><head><title>Jobs at GitLab</title></head><body>
<div class="job-posts"><table><tbody>
<tr class="job-post"><td class="cell">
  <a href="https://job-boards.greenhouse.io/gitlab/jobs/8860302002" target="_top">
    <p class="body body--medium">Senior Python Engineer</p>
    <p class="body body__secondary body--metadata">Remote, Europe</p>
  </a>
</td></tr>
<tr class="job-post"><td class="cell">
  <a href="https://job-boards.greenhouse.io/gitlab/jobs/8785825002" target="_top">
    <p class="body body--medium">Product Designer</p>
    <p class="body body__secondary body--metadata">New York, NY</p>
  </a>
</td></tr>
</tbody></table></div>
<nav aria-label="Pagination" class="pagination pagination--center">
  <button aria-label="Next page" type="button" class="pagination__btn pagination__next"></button>
</nav>
</body></html>
"""

LAST_PAGE_HTML = LISTING_HTML.replace(
    'class="pagination__btn pagination__next"',
    'class="pagination__btn pagination__next pagination__next--inactive"',
)

LEGACY_LISTING_HTML = """
<html><head><title>Jobs at GitLab</title></head><body>
<div id="content">
  <div class="opening">
    <a href="https://boards.greenhouse.io/gitlab/jobs/1111111">Data Engineer</a>
    <div class="location">Remote, US</div>
  </div>
</div>
</body></html>
"""

CARD_HTML = """
<html><head>
<title>Job Application for Senior Python Engineer at GitLab</title>
<meta property="og:title" content="Senior Python Engineer"/>
<meta property="og:description" content="Remote, Europe"/>
</head><body>
<div class="job__header">
  <div class="job__title">
    <h1 class="section-header section-header--large">Senior Python Engineer</h1>
    <div class="job__location"><svg width="24" height="24"></svg>Remote, Europe</div>
  </div>
</div>
<div class="job__description body">
  <div class="content-intro"><p>We build Python services.</p></div>
  <h2>Responsibilities</h2>
  <ul><li>Ship production code</li></ul>
</div>
</body></html>
"""

# --- вспомогательные функции -------------------------------------------------


def _response(status_code: int = 200, text: str = "", url: str = "") -> FetchResponse:
    """Фиктивный ответ сессии."""
    return FetchResponse(status_code=status_code, text=text, url=url)


def _api_response(payload: dict | None = None, url: str = API_URL) -> FetchResponse:
    """Успешный ответ Job Board API."""
    return _response(200, json.dumps(payload or API_PAYLOAD), url)


def _adapter(
    pages: dict[str, FetchResponse | Exception] | None = None,
    *,
    board_token: str | None = None,
) -> GreenhouseAdapter:
    """Адаптер с моком AntiBanSession (сеть не используется).

    Args:
        pages: URL → ответ (или исключение для имитации сбоя). URL без
            записи обрабатываются по умолчанию: API → успешный payload,
            всё остальное → 404.
    """
    session = Mock(spec=AntiBanSession)
    pages = dict(pages or {})

    async def mock_fetch(url: str, *, fetcher=None, dest="document", max_attempts=1):
        if url in pages:
            result = pages[url]
            if isinstance(result, Exception):
                raise result
            return result
        if url.startswith(API_BASE):
            return _api_response()
        return _response(404, "", url)

    session.fetch = mock_fetch
    return GreenhouseAdapter(session=session, board_token=board_token)


# --- тесты normalize ----------------------------------------------------------


def test_normalize_api_job():
    """Нормализация вакансии из payload Job Board API (каноническая схема §7)."""
    adapter = _adapter()
    normalized = adapter.normalize(API_JOB_PYTHON)

    assert normalized["source"] == "greenhouse"
    assert normalized["external_id"] == "8860302002"
    assert normalized["url"] == API_JOB_PYTHON["absolute_url"]
    assert normalized["title"] == "Senior Python Engineer"
    assert normalized["company_name"] == "GitLab"
    assert normalized["area"] == "Remote, Europe"
    assert normalized["published_at"] == "2026-09-30T09:00:00-04:00"
    assert normalized["description_html"] == "<p>We build <strong>Python</strong> services.</p>"
    assert normalized["description_raw"] == "We build Python services."
    assert normalized["work_format"] == "remote"  # локация содержит «Remote»
    assert normalized["salary_from"] is None
    assert normalized["salary_to"] is None
    assert normalized["salary_currency"] is None


def test_normalize_html_job_uses_preset_company():
    """HTML-fallback: строковая локация и company_name из пресета токена."""
    adapter = _adapter(board_token="gitlab")
    normalized = adapter.normalize(
        {"external_id": "1111111", "title": "Data Engineer", "location": "Remote, US"}
    )

    assert normalized["source"] == "greenhouse"
    assert normalized["external_id"] == "1111111"
    assert normalized["area"] == "Remote, US"
    assert normalized["work_format"] == "remote"
    assert normalized["company_name"] == PRESET_COMPANY_NAMES["gitlab"]
    assert normalized["url"] == job_url_for("gitlab", "1111111")


def test_normalize_missing_external_id_raises():
    """Без id нормализация невозможна."""
    adapter = _adapter()
    with pytest.raises(ValueError):
        adapter.normalize({"title": "No id"})


def test_normalize_invalid_board_token_raises():
    """Недопустимые символы в board_token → ValueError."""
    with pytest.raises(ValueError):
        normalize_board_token("bad token!")


# --- тесты search -------------------------------------------------------------


async def test_search_via_api():
    """Основной путь: список вакансий через Job Board API."""
    adapter = _adapter({API_URL: _api_response()})
    results = await adapter.search({})

    assert [r["external_id"] for r in results] == ["8860302002", "8785825002"]
    for result in results:
        assert result["source"] == "greenhouse"
        assert result["url"].startswith(JOB_BASE)


async def test_search_api_error_falls_back_to_html():
    """API недоступен → парсинг HTML-выдачи boards.greenhouse.io."""
    adapter = _adapter(
        {
            API_URL: Exception("api down"),
            LISTING_URL: _response(200, LISTING_HTML, LISTING_URL),
        }
    )
    results = await adapter.search({"max_pages": 3})

    assert [r["external_id"] for r in results] == ["8860302002", "8785825002"]
    assert results[0]["title"] == "Senior Python Engineer"
    assert results[0]["location"] == "Remote, Europe"
    assert results[0]["company_name"] == "GitLab"
    assert all(r["source"] == "greenhouse" for r in results)


async def test_search_api_invalid_json_falls_back_to_html():
    """API ответил не-JSON (200) → HTML-fallback."""
    adapter = _adapter(
        {
            API_URL: _response(200, "<html>blocked</html>", API_URL),
            LISTING_URL: _response(200, LISTING_HTML, LISTING_URL),
        }
    )
    results = await adapter.search({})

    assert [r["external_id"] for r in results] == ["8860302002", "8785825002"]


async def test_search_keyword_filter():
    """Клиентский фильтр keywords: только совпавшие вакансии."""
    adapter = _adapter({API_URL: _api_response()})
    results = await adapter.search({"keywords": ["python"]})

    assert [r["external_id"] for r in results] == ["8860302002"]


async def test_search_respects_board_token_filter():
    """Фильтр board_token переключает доску (URL строится под токен)."""
    airbnb_api = api_url_for("airbnb")
    adapter = _adapter(
        {
            API_URL: Exception("wrong board"),
            airbnb_api: _api_response(
                {"jobs": [{**API_JOB_PYTHON, "company_name": "Airbnb"}]},
                url=airbnb_api,
            ),
        }
    )
    results = await adapter.search({"board_token": "airbnb"})

    assert len(results) == 1
    assert results[0]["company_name"] == "Airbnb"


async def test_search_deduplicates_and_honors_limit():
    """Дубли id отсекаются, limit обрезает выдачу."""
    payload = {"jobs": [API_JOB_PYTHON, API_JOB_PYTHON, API_JOB_DESIGNER]}
    adapter = _adapter({API_URL: _api_response(payload)})
    results = await adapter.search({"limit": 1})

    assert len(results) == 1
    assert results[0]["external_id"] == "8860302002"


# --- тесты get_vacancy --------------------------------------------------------


async def test_get_vacancy_via_api():
    """Основной путь: одна вакансия через .../jobs/{id}?content=true."""
    adapter = _adapter({SINGLE_JOB_URL: _api_response(API_JOB_PYTHON, SINGLE_JOB_URL)})

    by_id = await adapter.get_vacancy("8860302002")
    assert by_id is not None
    assert by_id["source"] == "greenhouse"
    assert by_id["external_id"] == "8860302002"
    assert by_id["title"] == "Senior Python Engineer"

    by_url = await adapter.get_vacancy(API_JOB_PYTHON["absolute_url"])
    assert by_url is not None
    assert by_url["external_id"] == "8860302002"


async def test_get_vacancy_api_fails_falls_back_to_card_html():
    """API упал → карточка запрашивается по ссылке и разбирается из HTML."""
    adapter = _adapter(
        {
            SINGLE_JOB_URL: Exception("api down"),
            CARD_URL: _response(200, CARD_HTML, CARD_URL),
        }
    )
    result = await adapter.get_vacancy("8860302002")

    assert result is not None
    assert result["source"] == "greenhouse"
    assert result["external_id"] == "8860302002"
    assert result["title"] == "Senior Python Engineer"
    assert result["company_name"] == "GitLab"
    assert result["area"] == "Remote, Europe"
    assert "Ship production code" in result["description_raw"]


async def test_get_vacancy_not_found_returns_none():
    """API упал, карточка отдаёт 404 → None (docs/04 §5 not_found)."""
    missing_url = f"{API_BASE}/{TOKEN}/jobs/4444444?content=true"
    adapter = _adapter({missing_url: Exception("api down")})  # карточка → 404
    assert await adapter.get_vacancy("4444444") is None


async def test_get_vacancy_invalid_input_returns_none():
    """Мусор вместо id/ссылки → None без запросов."""
    adapter = _adapter()
    assert await adapter.get_vacancy("") is None
    assert await adapter.get_vacancy("not-a-job") is None


# --- HTML-парсеры -------------------------------------------------------------


def test_parse_api_jobs_rejects_wrong_payload():
    """Не-JSON / отсутствие «jobs» → ValueError."""
    with pytest.raises(ValueError):
        parse_api_jobs("<html>not json</html>")
    with pytest.raises(ValueError):
        parse_api_jobs('{"unexpected": []}')


def test_parse_html_jobs_new_markup():
    """Новая вёрстка: tr.job-post, заголовок и локация внутри ссылки."""
    jobs = parse_html_jobs(LISTING_HTML)

    assert [job["external_id"] for job in jobs] == ["8860302002", "8785825002"]
    assert jobs[0]["title"] == "Senior Python Engineer"
    assert jobs[0]["location"] == "Remote, Europe"
    assert jobs[0]["company_name"] == "GitLab"


def test_parse_html_jobs_legacy_markup():
    """Старая вёрстка: <div class="opening"> и локация-сосед ссылки."""
    jobs = parse_html_jobs(LEGACY_LISTING_HTML)

    assert len(jobs) == 1
    assert jobs[0]["external_id"] == "1111111"
    assert jobs[0]["title"] == "Data Engineer"
    assert jobs[0]["location"] == "Remote, US"


def test_has_next_page():
    """Пагинация: есть следующая / последняя страница."""
    assert has_next_page(LISTING_HTML) is True
    assert has_next_page(LAST_PAGE_HTML) is False
    assert has_next_page("<html>no pagination</html>") is False


def test_extract_card_fields_from_html():
    """Карточка: заголовок, компания, локация и сбалансированное описание."""
    fields = extract_card_fields(CARD_HTML)

    assert fields["title"] == "Senior Python Engineer"
    assert fields["company_name"] == "GitLab"
    assert fields["area"] == "Remote, Europe"
    # Вложенный content-intro не обрывает блок описания.
    assert "We build Python services." in fields["description_raw"]
    assert "Ship production code" in fields["description_raw"]
    assert fields["description_html"].startswith('<div class="content-intro">')


def test_extract_card_fields_from_remix_context():
    """Нет серверной разметки → данные из window.__remixContext."""
    html = (
        '<html><head><title>Job Application for Backend Engineer at GitLab</title></head>'
        "<body><script>window.__remixContext = "
        + json.dumps(
            {
                "state": {
                    "loaderData": {
                        "routes/$url_token_.jobs_.$job_post_id": {
                            "jobPost": {
                                "title": "Backend Engineer",
                                "content": "<p>Build APIs.</p>",
                            }
                        }
                    }
                }
            }
        )
        + ";</script></body></html>"
    )
    fields = extract_card_fields(html)

    assert fields["title"] == "Backend Engineer"
    assert fields["company_name"] == "GitLab"
    assert fields["description_raw"] == "Build APIs."


# --- SourceRegistry и хуки оркестратора --------------------------------------


def test_default_registry_registers_greenhouse():
    """default_registry() содержит hh и greenhouse; create() отдаёт экземпляр."""
    registry = default_registry()

    assert "hh" in registry.names()
    assert "greenhouse" in registry.names()

    adapter = registry.create("greenhouse")
    assert isinstance(adapter, GreenhouseAdapter)
    assert adapter.source_name == "greenhouse"


def test_preset_board_tokens():
    """Пресет токенов непуст, у каждого токена есть имя компании."""
    assert PRESET_BOARD_TOKENS
    for token in PRESET_BOARD_TOKENS:
        assert token in PRESET_COMPANY_NAMES

    # Без явного токена берётся первый элемент пресета.
    adapter = _adapter()
    assert adapter.board_token == PRESET_BOARD_TOKENS[0]


def test_orchestrator_hooks_build_search_url():
    """Ссылка автопоиска — API доски (дефолт пресета + явный board_token)."""
    adapter = _adapter()

    assert adapter.build_search_url(keywords=["python"]) == api_url_for(
        PRESET_BOARD_TOKENS[0]
    )
    assert adapter.build_search_url(board_token="figma") == api_url_for("figma")
    explicit = _adapter(board_token="airbnb")
    assert explicit.build_search_url() == api_url_for("airbnb")


def test_orchestrator_hooks_validate_search_url():
    """Белый список хостов Greenhouse (docs/04 §4.2)."""
    adapter = _adapter()

    assert adapter.validate_search_url(LISTING_URL) == LISTING_URL
    assert adapter.validate_search_url(API_URL) == API_URL
    assert adapter.validate_search_url(CARD_URL) == CARD_URL
    with pytest.raises(ValueError):
        adapter.validate_search_url("https://evil.example.com/jobs")


def test_orchestrator_hooks_page_url():
    """API без пагинации; HTML-выдача обходит ?page=N."""
    adapter = _adapter()

    assert adapter.page_url(API_URL, 0) == API_URL
    assert adapter.page_url(API_URL, 3) == API_URL  # API отдаёт всё разом
    assert adapter.page_url(LISTING_URL, 0) == LISTING_URL
    assert adapter.page_url(LISTING_URL, 1) == f"{LISTING_URL}?page=2"


def test_orchestrator_hooks_parse_listing_json():
    """parse_listing понимает JSON-ответ API: (id, без пагинации)."""
    adapter = _adapter()

    ids, has_next = adapter.parse_listing(json.dumps(API_PAYLOAD))
    assert ids == ["8860302002", "8785825002"]
    assert has_next is False


def test_orchestrator_hooks_parse_listing_html():
    """parse_listing понимает HTML-фолбэк: (id, признак следующей страницы)."""
    adapter = _adapter()

    ids, has_next = adapter.parse_listing(LISTING_HTML)
    assert ids == ["8860302002", "8785825002"]
    assert has_next is True

    ids, has_next = adapter.parse_listing(LAST_PAGE_HTML)
    assert ids == ["8860302002", "8785825002"]
    assert has_next is False


def test_orchestrator_hooks_build_vacancy_url():
    """Прямая ссылка карточки строится из ссылки выдачи и id."""
    adapter = _adapter()

    assert adapter.build_vacancy_url(API_URL, "8860302002") == CARD_URL
    assert adapter.build_vacancy_url(LISTING_URL, "8785825002") == job_url_for(
        TOKEN, "8785825002"
    )
    # Токен берётся из ссылки выдачи, а не из доски по умолчанию.
    assert adapter.build_vacancy_url(api_url_for("figma"), "1") == job_url_for(
        "figma", "1"
    )


def test_orchestrator_hooks_extract_fields():
    """extract_fields отдаёт контент-поля карточки (title/company/area/desc)."""
    adapter = _adapter()
    fields = adapter.extract_fields(CARD_HTML)

    assert fields["title"] == "Senior Python Engineer"
    assert fields["company_name"] == "GitLab"
    assert fields["area"] == "Remote, Europe"
    assert "Ship production code" in fields["description_raw"]


def test_greenhouse_api_error_exists():
    """GreenhouseAPIError — наследник RuntimeError (контракт ошибок источника)."""
    assert issubclass(GreenhouseAPIError, RuntimeError)
    assert "greenhouse.io" in LISTING_BASE
