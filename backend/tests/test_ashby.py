"""Тесты адаптера Ashby: normalize, search (API-мок) и HTML-hydration fallback.

Покрывают docs/04_PARSING_RULES.md §10 (адаптер источника, SourceRegistry):

- ``normalize`` — маппинг payload Posting API (включая ``compensation``) в
  каноническую схему §7;
- ``search`` — основной путь через ``api.ashbyhq.com`` и клиентский фильтр
  ``keywords``/``isListed``;
- fallback — при отказе/непригодности API разбор hydration ``window.__appData``
  страницы ``jobs.ashbyhq.com/{board}``;
- ``get_vacancy`` — одна вакансия через API (поиск по id) и через HTML-карточку;
- регистрация ``AshbyAdapter`` в ``default_registry()``;
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
from app.modules.parsing.sources.ashby import (
    API_BASE,
    LISTING_BASE,
    PRESET_COMPANY_NAMES,
    PRESET_JOB_BOARDS,
    AshbyAdapter,
    AshbyAPIError,
    api_url_for,
    extract_card_fields,
    extract_salary_range,
    has_next_page,
    job_url_for,
    listing_url_for,
    normalize_job_board,
    parse_api_jobs,
    parse_html_jobs,
)

#: Доска тестовой компании.
BOARD = "Ashby"
API_URL = api_url_for(BOARD)
LISTING_URL = listing_url_for(BOARD)
JOB_ID = "6a6184ee-abc9-4a58-8ca3-4221d809a9ab"
OTHER_ID = "7458d4e9-da2e-47bd-98cb-adfda43d42b2"
CARD_URL = job_url_for(BOARD, JOB_ID)

# --- фикстуры payload API ----------------------------------------------------

API_JOB_PYTHON = {
    "id": JOB_ID,
    "title": "Senior Python Engineer",
    "location": "Remote - European Union",
    "department": "Engineering",
    "team": "Platform",
    "employmentType": "FullTime",
    "workplaceType": "Remote",
    "isRemote": True,
    "isListed": True,
    "publishedAt": "2026-09-30T09:00:00.000+00:00",
    "descriptionHtml": "<p>We build <strong>Python</strong> services.</p>",
    "descriptionPlain": "We build Python services.",
    "jobUrl": "https://jobs.ashbyhq.com/Ashby/6a6184ee-abc9-4a58-8ca3-4221d809a9ab",
    "applyUrl": "https://jobs.ashbyhq.com/Ashby/6a6184ee-abc9-4a58-8ca3-4221d809a9ab/apply",
    "compensation": {
        "compensationTierSummary": "$120K – $165K • Offers Equity",
        "scrapeableCompensationSalarySummary": "$120K - $165K",
        "summaryComponents": [
            {
                "compensationType": "EquityPercentage",
                "interval": "NONE",
                "currencyCode": None,
                "minValue": 0.5,
                "maxValue": 1.75,
            },
            {
                "compensationType": "Salary",
                "interval": "1 YEAR",
                "currencyCode": "USD",
                "minValue": 120000,
                "maxValue": 165000,
            },
        ],
    },
}

API_JOB_DESIGNER = {
    "id": OTHER_ID,
    "title": "Product Designer",
    "location": "New York, NY",
    "employmentType": "PartTime",
    "workplaceType": "OnSite",
    "isRemote": False,
    "isListed": True,
    "publishedAt": "2026-09-29T10:36:33.000+00:00",
    "descriptionHtml": "<p>Design delightful interfaces.</p>",
    "descriptionPlain": "Design delightful interfaces.",
    "jobUrl": "https://jobs.ashbyhq.com/Ashby/7458d4e9-da2e-47bd-98cb-adfda43d42b2",
    "applyUrl": "https://jobs.ashbyhq.com/Ashby/7458d4e9-da2e-47bd-98cb-adfda43d42b2/apply",
    "compensation": {
        "compensationTierSummary": "€60K – €80K",
        "summaryComponents": [
            {
                "compensationType": "Salary",
                "interval": "1 YEAR",
                "currencyCode": "EUR",
                "minValue": 60000,
                "maxValue": 80000,
            },
        ],
    },
}

#: Вакансия только по прямой ссылке (не должна попадать в выдачу).
API_JOB_UNLISTED = {
    "id": "11111111-2222-3333-4444-555555555555",
    "title": "Confidential Role",
    "location": "Remote",
    "isListed": False,
    "jobUrl": "https://jobs.ashbyhq.com/Ashby/11111111-2222-3333-4444-555555555555",
}

API_PAYLOAD = {"apiVersion": "1", "jobs": [API_JOB_PYTHON, API_JOB_DESIGNER]}

# --- фикстуры HTML (fallback и карточка) -------------------------------------

#: Hydration выдачи: ``window.__appData`` c ``jobBoard``/``organization``.
LISTING_HTML = """
<html><head><title>Ashby Jobs</title></head><body>
<div id="root"></div>
<script nonce="abc">window.__appData = """ + json.dumps(
    {
        "organization": {"name": "Ashby", "hostedJobsPageSlug": "Ashby"},
        "posting": None,
        "jobBoard": {
            "teams": [
                {"id": "team-1", "name": "Engineering"},
                {"id": "team-2", "name": "Design"},
            ],
            "jobPostings": [
                {
                    "id": JOB_ID,
                    "title": "Senior Python Engineer",
                    "teamId": "team-1",
                    "locationName": "Remote - European Union",
                    "workplaceType": "Remote",
                    "employmentType": "FullTime",
                    "compensationTierSummary": "$120K – $165K • Offers Equity",
                },
                {
                    "id": OTHER_ID,
                    "title": "Product Designer",
                    "teamId": "team-2",
                    "locationName": "New York, NY",
                    "workplaceType": "OnSite",
                    "employmentType": "PartTime",
                    "compensationTierSummary": "€60K – €80K",
                },
            ],
        },
    },
    ensure_ascii=False,
) + """
</script>
</body></html>
"""

#: Hydration карточки: ``window.__appData`` c ``posting``/``organization``.
CARD_HTML = """
<html><head>
<title>Senior Python Engineer - Ashby</title>
<meta property="og:title" content="Senior Python Engineer"/>
</head><body>
<div id="root"></div>
<script nonce="def">window.__appData = """ + json.dumps(
    {
        "organization": {"name": "Ashby", "hostedJobsPageSlug": "Ashby"},
        "posting": {
            "id": JOB_ID,
            "title": "Senior Python Engineer",
            "locationName": "Remote - European Union",
            "workplaceType": "Remote",
            "employmentType": "FullTime",
            "descriptionHtml": "<p>We build <strong>Python</strong> services.</p>",
            "scrapeableCompensationSalarySummary": "$120K - $165K",
            "compensationTierSummary": "$120K – $165K • Offers Equity",
        },
    },
    ensure_ascii=False,
) + """
</script>
</body></html>
"""

# --- вспомогательные функции -------------------------------------------------


def _response(status_code: int = 200, text: str = "", url: str = "") -> FetchResponse:
    """Фиктивный ответ сессии."""
    return FetchResponse(status_code=status_code, text=text, url=url)


def _api_response(payload: dict | None = None, url: str = API_URL) -> FetchResponse:
    """Успешный ответ Posting API."""
    return _response(200, json.dumps(payload or API_PAYLOAD), url)


def _adapter(
    pages: dict[str, FetchResponse | Exception] | None = None,
    *,
    job_board: str | None = None,
) -> AshbyAdapter:
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
    return AshbyAdapter(session=session, job_board=job_board)


# --- тесты normalize ----------------------------------------------------------


def test_normalize_api_job():
    """Нормализация вакансии из payload Posting API (каноническая схема §7)."""
    adapter = _adapter()
    normalized = adapter.normalize(API_JOB_PYTHON)

    assert normalized["source"] == "ashby"
    assert normalized["external_id"] == JOB_ID
    assert normalized["url"] == API_JOB_PYTHON["jobUrl"]
    assert normalized["title"] == "Senior Python Engineer"
    assert normalized["area"] == "Remote - European Union"
    assert normalized["published_at"] == "2026-09-30T09:00:00.000+00:00"
    assert normalized["description_html"] == "<p>We build <strong>Python</strong> services.</p>"
    assert normalized["description_raw"] == "We build Python services."
    # Формат работы и форма занятости из enum'ов Ashby.
    assert normalized["work_format"] == "remote"
    assert normalized["employment_form"] == "Full-time"
    # Зарплата из структурированного compensation (Salary-компонент).
    assert normalized["salary_from"] == 120000
    assert normalized["salary_to"] == 165000
    assert normalized["salary_currency"] == "USD"


def test_normalize_api_job_uses_preset_company():
    """Компания из пресета доски, когда API её не отдаёт."""
    adapter = _adapter(job_board="Ashby")
    normalized = adapter.normalize(API_JOB_DESIGNER)

    assert normalized["source"] == "ashby"
    assert normalized["external_id"] == OTHER_ID
    assert normalized["company_name"] == "Ashby"
    assert normalized["work_format"] == "onsite"
    assert normalized["employment_form"] == "Part-time"
    assert normalized["salary_from"] == 60000
    assert normalized["salary_to"] == 80000
    assert normalized["salary_currency"] == "EUR"


def test_normalize_html_card_uses_salary_summary():
    """HTML-карточка: строковая зарплата и company_name из hydration."""
    adapter = _adapter()
    normalized = adapter.normalize(
        extract_card_fields(CARD_HTML) | {"external_id": JOB_ID, "url": CARD_URL}
    )

    assert normalized["source"] == "ashby"
    assert normalized["external_id"] == JOB_ID
    assert normalized["company_name"] == "Ashby"
    assert normalized["salary_from"] == 120000
    assert normalized["salary_to"] == 165000
    assert normalized["salary_currency"] == "USD"
    assert normalized["work_format"] == "remote"
    assert normalized["employment_form"] == "Full-time"


def test_normalize_missing_external_id_raises():
    """Нет id → ValueError (контракт §7)."""
    adapter = _adapter()
    with pytest.raises(ValueError):
        adapter.normalize({"title": "No id"})


def test_extract_salary_range_parses_ashby_summaries():
    """Парс строковых summary Ashby: тире, K-суффикс, bullet, валюты."""
    assert extract_salary_range("$120K – $165K • Offers Equity") == (120000, 165000, "USD")
    assert extract_salary_range("$120K - $165K") == (120000, 165000, "USD")
    assert extract_salary_range("$60,000 - $80,000") == (60000, 80000, "USD")
    assert extract_salary_range("€60K – €80K") == (60000, 80000, "EUR")
    assert extract_salary_range("£50K") == (50000, 50000, "GBP")
    assert extract_salary_range(None) == (None, None, None)
    assert extract_salary_range("Открыта вакансия") == (None, None, None)


# --- тесты search -------------------------------------------------------------


async def test_search_via_api():
    """Основной путь: выдача собирается из Posting API."""
    adapter = _adapter({API_URL: _api_response()})
    results = await adapter.search({})

    assert [r["external_id"] for r in results] == [JOB_ID, OTHER_ID]
    assert results[0]["source"] == "ashby"
    assert results[0]["url"] == API_JOB_PYTHON["jobUrl"]
    assert results[0]["company_name"] == "Ashby"
    # Salary-компонент доступен для дальнейшей нормализации.
    assert results[0]["compensation"]["summaryComponents"][1]["minValue"] == 120000


async def test_search_api_fails_falls_back_to_html_hydration():
    """API отдал не-JSON → разбор hydration ``window.__appData`` выдачи."""
    adapter = _adapter(
        {
            API_URL: _response(200, "<html>blocked</html>", API_URL),
            LISTING_URL: _response(200, LISTING_HTML, LISTING_URL),
        }
    )
    results = await adapter.search({})

    assert [r["external_id"] for r in results] == [JOB_ID, OTHER_ID]
    # Из hydration берутся локация, команда, компания и ссылка карточки.
    assert results[0]["team"] == "Engineering"
    assert results[0]["company_name"] == "Ashby"
    assert results[0]["url"] == CARD_URL
    assert results[0]["compensationTierSummary"] == "$120K – $165K • Offers Equity"


async def test_search_keyword_filter():
    """Клиентский фильтр keywords: только совпавшие вакансии."""
    adapter = _adapter({API_URL: _api_response()})
    results = await adapter.search({"keywords": ["python"]})

    assert [r["external_id"] for r in results] == [JOB_ID]


async def test_search_skips_unlisted_jobs():
    """Вакансии ``isListed=false`` (только прямая ссылка) не попадают в выдачу."""
    payload = {"jobs": [API_JOB_PYTHON, API_JOB_UNLISTED, API_JOB_DESIGNER]}
    adapter = _adapter({API_URL: _api_response(payload)})
    results = await adapter.search({})

    assert [r["external_id"] for r in results] == [JOB_ID, OTHER_ID]


async def test_search_respects_job_board_filter():
    """Фильтр job_board переключает доску (URL строится под имя)."""
    linear_api = api_url_for("Linear")
    adapter = _adapter(
        {
            API_URL: Exception("wrong board"),
            linear_api: _api_response(
                {"jobs": [{**API_JOB_PYTHON, "jobUrl": "https://jobs.ashbyhq.com/Linear/" + JOB_ID}]},
                url=linear_api,
            ),
        }
    )
    results = await adapter.search({"job_board": "Linear"})

    assert len(results) == 1
    assert results[0]["url"].startswith("https://jobs.ashbyhq.com/Linear/")


async def test_search_deduplicates_and_honors_limit():
    """Дубли id отсекаются, limit обрезает выдачу."""
    payload = {"jobs": [API_JOB_PYTHON, API_JOB_PYTHON, API_JOB_DESIGNER]}
    adapter = _adapter({API_URL: _api_response(payload)})
    results = await adapter.search({"limit": 1})

    assert len(results) == 1
    assert results[0]["external_id"] == JOB_ID


# --- тесты get_vacancy --------------------------------------------------------


async def test_get_vacancy_via_api():
    """Основной путь: одна вакансия ищется по id в ответе Posting API."""
    adapter = _adapter({API_URL: _api_response(API_PAYLOAD, API_URL)})

    by_id = await adapter.get_vacancy(JOB_ID)
    assert by_id is not None
    assert by_id["source"] == "ashby"
    assert by_id["external_id"] == JOB_ID
    assert by_id["title"] == "Senior Python Engineer"
    assert by_id["compensation"]["summaryComponents"][1]["maxValue"] == 165000

    by_url = await adapter.get_vacancy(API_JOB_PYTHON["jobUrl"])
    assert by_url is not None
    assert by_url["external_id"] == JOB_ID


async def test_get_vacancy_api_fails_falls_back_to_card_html():
    """API упал → карточка запрашивается по ссылке и разбирается из hydration."""
    adapter = _adapter(
        {
            API_URL: Exception("api down"),
            CARD_URL: _response(200, CARD_HTML, CARD_URL),
        }
    )
    result = await adapter.get_vacancy(JOB_ID)

    assert result is not None
    assert result["source"] == "ashby"
    assert result["external_id"] == JOB_ID
    assert result["title"] == "Senior Python Engineer"
    assert result["company_name"] == "Ashby"
    assert result["area"] == "Remote - European Union"
    assert result["salary_from"] == 120000
    assert "We build" in (result["description_raw"] or "")


async def test_get_vacancy_api_ok_but_id_missing_falls_back():
    """API ответил, но id нет (удалён) → HTML-карточка, иначе None."""
    adapter = _adapter(
        {
            API_URL: _api_response({"jobs": [API_JOB_DESIGNER]}, API_URL),
            CARD_URL: _response(200, CARD_HTML, CARD_URL),
        }
    )
    result = await adapter.get_vacancy(JOB_ID)
    assert result is not None
    assert result["external_id"] == JOB_ID


async def test_get_vacancy_not_found_returns_none():
    """API упал, карточка отдаёт 404 → None (docs/04 §5 not_found)."""
    missing = job_url_for(BOARD, JOB_ID)
    adapter = _adapter({API_URL: Exception("api down"), missing: _response(404, "", missing)})
    # карточка → 404
    assert await adapter.get_vacancy(JOB_ID) is None


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


def test_parse_html_jobs_from_hydration():
    """Разбор выдачи из ``window.__appData``: команды, компания, ссылки."""
    jobs = parse_html_jobs(LISTING_HTML)

    assert [job["external_id"] for job in jobs] == [JOB_ID, OTHER_ID]
    assert jobs[0]["title"] == "Senior Python Engineer"
    assert jobs[0]["location"] == "Remote - European Union"
    assert jobs[0]["team"] == "Engineering"
    assert jobs[0]["company_name"] == "Ashby"
    assert jobs[0]["url"] == CARD_URL
    assert jobs[1]["team"] == "Design"


def test_parse_html_jobs_no_payload_returns_empty():
    """Нет hydration → пустой список (не исключение)."""
    assert parse_html_jobs("<html><body>Загрузка…</body></html>") == []


def test_has_next_page_is_false():
    """Ashby не использует пагинацию: признак всегда False."""
    assert has_next_page(LISTING_HTML) is False
    assert has_next_page("<html>no data</html>") is False


def test_extract_card_fields_from_hydration():
    """Карточка: поля из ``posting`` плюс компания из ``organization``."""
    fields = extract_card_fields(CARD_HTML)

    assert fields["title"] == "Senior Python Engineer"
    assert fields["company_name"] == "Ashby"
    assert fields["area"] == "Remote - European Union"
    assert fields["work_format"] == "remote"
    assert fields["employment_form"] == "Full-time"
    assert fields["salary_from"] == 120000
    assert fields["salary_to"] == 165000
    assert "We build Python services." in fields["description_raw"]
    assert fields["description_html"].startswith("<p>")


def test_extract_card_fields_title_fallback():
    """Нет ``posting`` → заголовок из og:title/``<title>``."""
    html = (
        '<html><head><title>Backend Engineer - Ashby</title>'
        '<meta property="og:title" content="Backend Engineer"/></head>'
        "<body>Нет гидратации</body></html>"
    )
    fields = extract_card_fields(html)

    assert fields["title"] == "Backend Engineer"


# --- SourceRegistry и хуки оркестратора --------------------------------------


def test_default_registry_registers_ashby():
    """default_registry() содержит hh и ashby; create() отдаёт экземпляр."""
    registry = default_registry()

    assert "hh" in registry.names()
    assert "ashby" in registry.names()

    adapter = registry.create("ashby")
    assert isinstance(adapter, AshbyAdapter)
    assert adapter.source_name == "ashby"


def test_preset_boards_have_company_names():
    """Пресет непуст, у каждой доски есть имя компании."""
    assert PRESET_JOB_BOARDS
    for board in PRESET_JOB_BOARDS:
        assert board in PRESET_COMPANY_NAMES


def test_orchestrator_hooks_build_search_url():
    """Ссылка автопоиска — Posting API доски (дефолт пресета + явный job_board)."""
    adapter = _adapter()

    assert adapter.build_search_url(keywords=["python"]) == api_url_for("Ashby")
    assert adapter.build_search_url(job_board="Linear") == api_url_for("Linear")
    explicit = _adapter(job_board="ramp")
    assert explicit.build_search_url() == api_url_for("ramp")


def test_orchestrator_hooks_validate_search_url():
    """Белый список хостов Ashby (docs/04 §4.2)."""
    adapter = _adapter()

    assert adapter.validate_search_url(API_URL) == API_URL
    assert adapter.validate_search_url(LISTING_URL) == LISTING_URL
    assert adapter.validate_search_url(CARD_URL) == CARD_URL
    with pytest.raises(ValueError):
        adapter.validate_search_url("https://evil.example.com/jobs")


def test_orchestrator_hooks_page_url():
    """Ashby без пагинации: base_url возвращается как есть."""
    adapter = _adapter()

    assert adapter.page_url(API_URL, 0) == API_URL
    assert adapter.page_url(API_URL, 3) == API_URL
    assert adapter.page_url(LISTING_URL, 1) == LISTING_URL


def test_orchestrator_hooks_parse_listing_json():
    """parse_listing понимает JSON-ответ API: (id, без пагинации)."""
    adapter = _adapter()

    ids, has_next = adapter.parse_listing(json.dumps(API_PAYLOAD))
    assert ids == [JOB_ID, OTHER_ID]
    assert has_next is False


def test_orchestrator_hooks_parse_listing_html():
    """parse_listing понимает HTML-hydration фолбэк: (id, без пагинации)."""
    adapter = _adapter()

    ids, has_next = adapter.parse_listing(LISTING_HTML)
    assert ids == [JOB_ID, OTHER_ID]
    assert has_next is False


def test_orchestrator_hooks_build_vacancy_url():
    """Прямая ссылка карточки строится из ссылки выдачи и id."""
    adapter = _adapter()

    assert adapter.build_vacancy_url(API_URL, JOB_ID) == CARD_URL
    assert adapter.build_vacancy_url(LISTING_URL, OTHER_ID) == job_url_for(
        BOARD, OTHER_ID
    )
    # Имя доски берётся из ссылки выдачи, а не из доски по умолчанию.
    assert adapter.build_vacancy_url(api_url_for("Linear"), JOB_ID) == job_url_for(
        "Linear", JOB_ID
    )


def test_orchestrator_hooks_extract_fields():
    """extract_fields отдаёт контент-поля карточки (title/company/area/desc)."""
    adapter = _adapter()
    fields = adapter.extract_fields(CARD_HTML)

    assert fields["title"] == "Senior Python Engineer"
    assert fields["company_name"] == "Ashby"
    assert fields["area"] == "Remote - European Union"
    assert "We build" in fields["description_raw"]


def test_normalize_job_board_validation():
    """Валидация имени доски: символы участвуют в URL."""
    assert normalize_job_board("Ashby") == "Ashby"
    assert normalize_job_board(" linear ") == "linear"
    assert normalize_job_board(None) is None
    assert normalize_job_board("") is None
    with pytest.raises(ValueError):
        normalize_job_board("bad/board")


def test_ashby_api_error_exists():
    """AshbyAPIError — наследник RuntimeError (контракт ошибок источника)."""
    assert issubclass(AshbyAPIError, RuntimeError)
    assert "api.ashbyhq.com" in API_BASE
    assert "jobs.ashbyhq.com" in LISTING_BASE
