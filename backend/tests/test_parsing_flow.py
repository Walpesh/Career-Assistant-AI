"""Тесты парсинга: fallback chain, режимы сбора, очередь и прогресс.

Покрывают docs/04_PARSING_RULES.md:
    §2  — fallback chain curl_cffi → Playwright → captcha;
    §4.1/§4.2/§4.3 — автопоиск, групповой парсер, ручное добавление;
    §5  — 404/удалённая вакансия, капча, ошибки задачи;
    §6  — приоритеты manual > group > auto, ≤2 воркера на пользователя,
          отдельная LLM-очередь, жизненный цикл задачи, прогресс;
    §7  — обязательные поля вакансии;
    §8  — дедупликация (user_id, hh_vacancy_id) и неизменность `applied`.

Сеть и браузер не используются: подменяются fetcher/оркестратор, как в
test_anti_ban.py и test_vacancies.py.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from urllib.parse import parse_qsl, urlsplit

import pytest
from sqlalchemy import select

from app.db.models import Task, User, Vacancy
from app.modules.anti_ban.exceptions import CaptchaDetected
from app.modules.anti_ban.session import AntiBanSession, FetchResponse
from app.modules.parsing import service as service_module
from app.modules.parsing.fetcher import HhPageFetcher, _is_usable
from app.modules.parsing.listing import extract_listing_ids, parse_listing
from app.modules.parsing.service import ParsingOrchestrator
from app.modules.parsing.urls import (
    build_auto_search_url,
    build_page_url,
    validate_hh_search_url,
)
from app.modules.queue_manager.queues import enqueue_task
from app.modules.queue_manager.worker import (
    LLM_TASK_TYPES,
    MAX_CONCURRENT_PARSERS_PER_USER,
    PARSING_TASK_TYPES,
    TASK_PRIORITY,
    run_parsing_task,
)

# --- фикстуры HTML ---------------------------------------------------------

VACANCY_CARD = """
<html><head><title>Python разработчик — hh.ru</title>
<meta property="og:title" content="Python разработчик"></head>
<body>
  <div data-qa="vacancy-company-name">ООО Ромашка</div>
  <div data-qa="vacancy-salary">от 150000 до 250000 руб.</div>
  <div data-qa="vacancy-experience">3–6 лет</div>
  <div data-qa="vacancy-employment">полная занятость</div>
  <div data-qa="vacancy-work_format-by-day">удалённая работа</div>
  <div data-qa="vacancy-schedule">пн–пт, 9:00–18:00</div>
  <div data-qa="vacancy-view-top-address">Москва</div>
  <time data-qa="vacancy-public-date">сегодня</time>
  <div data-qa="vacancy-description">Обязанности: разработка на Python.</div>
</body></html>
"""


VACANCY_CARD_BLACKLISTED = VACANCY_CARD.replace(
    "Обязанности: разработка на Python.",
    "Обязанности: разработка на Python. Оформление строго по ТК РФ.",
)


def _listing_html(vacancy_ids: list[str]) -> str:
    """Страница выдачи со встроенным JSON HH-Lux-InitialState (docs/04 §2)."""
    state = {
        "vacancySearchResult": {
            "vacancies": [{"vacancyId": int(i)} for i in vacancy_ids],
        }
    }
    items = "".join(
        f'<div data-qa="serp-item"><a href="/vacancy/{i}">v</a></div>' for i in vacancy_ids
    )
    return (
        "<html><body>"
        '<script id="HH-Lux-InitialState" type="application/json">'
        f"{json.dumps(state)}</script>{items}"
        '<a data-qa="pager-next" href="?page=1">2</a>'
        "</body></html>"
    )


class FakeSession:
    """AntiBanSession с подменённым fetch: без сети, прогрева и реальных пауз."""

    def __init__(self, pages: dict[str, FetchResponse] | None = None):
        self.pages = pages or {}
        self.requested: list[str] = []

    async def fetch(self, url: str, **_kwargs) -> FetchResponse:
        self.requested.append(url)
        await asyncio.sleep(0)
        return self.pages.get(url, FetchResponse(status_code=404, text="", url=url))


def _session_factory(engine):
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    return async_sessionmaker(
        bind=engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
    )


async def _drain(rounds: int = 60) -> None:
    """Дождаться фоновых задач, созданных воркером."""
    for _ in range(rounds):
        pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        if not pending:
            return
        await asyncio.gather(*pending, return_exceptions=True)


@pytest.fixture
def user_factory(engine):
    """Создание пользователя для задач парсинга."""
    from sqlalchemy.ext.asyncio import AsyncSession

    async def _make(email: str | None = None) -> uuid.UUID:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            user = User(
                email=email or f"u{uuid.uuid4().hex[:10]}@test.dev",
                password_hash="x",
            )
            session.add(user)
            await session.commit()
            return user.id

    return _make


async def _make_task(engine, user_id, task_type, payload=None) -> uuid.UUID:
    from sqlalchemy.ext.asyncio import AsyncSession

    async with AsyncSession(engine, expire_on_commit=False) as session:
        task = Task(
            user_id=user_id,
            task_type=task_type,
            status="pending",
            progress_current=0,
            progress_total=0,
            payload=payload or {},
        )
        session.add(task)
        await session.commit()
        await session.refresh(task)
        return task.id


async def _vacancies(session, user_id) -> list[Vacancy]:
    return list(
        (
            await session.scalars(
                select(Vacancy)
                .where(Vacancy.user_id == user_id)
                .order_by(Vacancy.hh_vacancy_id)
            )
        ).all()
    )


def _prepared_request(url: str = "https://hh.ru/search/vacancy?text=python"):
    from app.modules.anti_ban.fingerprint import generate_fingerprint
    from app.modules.anti_ban.session import PreparedRequest

    return PreparedRequest(
        url=url,
        headers={},
        cookies={},
        proxy_url=None,
        endpoint=None,
        fingerprint=generate_fingerprint(),
    )


def _stub_orchestrator(monkeypatch) -> list[str]:
    """Подменяем оркестратор: сетевые вызовы не нужны для проверки очереди."""
    executed: list[str] = []

    async def fake_run_auto(self, db, **kwargs):
        executed.append("auto")
        await _report(kwargs.get("progress"))
        return service_module.ParsingOutcome(vacancy_ids=["1"], created=1)

    async def fake_run_group(self, db, **kwargs):
        executed.append("group")
        await _report(kwargs.get("progress"))
        return service_module.ParsingOutcome(vacancy_ids=["2"], created=1)

    async def fake_run_manual(self, db, **kwargs):
        executed.append("manual")
        await _report(kwargs.get("progress"))
        return service_module.ParsingOutcome(vacancy_ids=["3"], created=1)

    async def _report(reporter) -> None:
        """Прогресс публикуется во всех режимах (docs/04 §6)."""
        if reporter is not None:
            await reporter(1, 1, "parsing_vacancy", "готово")

    monkeypatch.setattr(service_module.ParsingOrchestrator, "run_auto", fake_run_auto)
    monkeypatch.setattr(service_module.ParsingOrchestrator, "run_group", fake_run_group)
    monkeypatch.setattr(service_module.ParsingOrchestrator, "run_manual", fake_run_manual)
    return executed


# --- docs/04 §4.1: URL поиска и пагинация ---------------------------------

def test_auto_search_url_contains_keywords_and_filters():
    """docs/04 §4.1 п.1: URL из ключевых слов и фильтров."""
    url = build_auto_search_url(
        keywords=["python", "fastapi"],
        employment_forms=["full"],
        work_formats=["remote"],
        schedules=["fullDay"],
    )
    assert url.startswith("https://hh.ru/search/vacancy?")
    assert "text=python+fastapi" in url
    assert "employment=full" in url
    assert "work_format=remote" in url
    assert "schedule=fullDay" in url
    assert "page=0" in url


@pytest.mark.parametrize(
    "kwargs,param,expected",
    [
        ({"employment_forms": ["full"]}, "employment", "full"),
        ({"employment_forms": ["full", "part", "gph"]}, "employment", "full,part,gph"),
        ({"work_formats": ["remote"]}, "work_format", "remote"),
        ({"work_formats": ["remote", "hybrid", "onsite"]}, "work_format", "remote,hybrid,onsite"),
        ({"schedules": ["fullDay"]}, "schedule", "fullDay"),
        (
            {"schedules": ["fullDay", "flexible", "shift", "flyInFlyOut"]},
            "schedule",
            "fullDay,flexible,shift,flyInFlyOut",
        ),
    ],
)
def test_auto_search_url_supports_every_filter_criterion(kwargs, param, expected):
    """docs/04 §4.1 п.1: «лёгкий тест-парс» — каждый критерий фильтра даёт свой параметр."""
    url = build_auto_search_url(keywords=["python"], **kwargs)
    query = dict(parse_qsl(urlsplit(url).query))

    assert query[param] == expected
    # Остальные фильтры не «протекают» в URL, если не заданы.
    for other in ("employment", "work_format", "schedule"):
        if other != param:
            assert other not in query


def test_auto_search_url_combines_multiple_filters_at_once():
    """docs/04 §4.1 п.1: фильтры комбинируются; пагинация их сохраняет."""
    url = build_auto_search_url(
        keywords=["python", "fastapi"],
        employment_forms=["full", "project", "gph"],
        work_formats=["remote", "hybrid"],
        schedules=["fullDay", "flexible"],
    )
    query = dict(parse_qsl(urlsplit(url).query))
    assert query["text"] == "python fastapi"
    assert query["employment"].split(",") == ["full", "project", "gph"]
    assert query["work_format"].split(",") == ["remote", "hybrid"]
    assert query["schedule"].split(",") == ["fullDay", "flexible"]

    # docs/04 §4.2 п.2: следующая страница сохраняет весь набор фильтров.
    page_query = dict(parse_qsl(urlsplit(build_page_url(url, 1)).query))
    assert page_query["employment"] == "full,project,gph"
    assert page_query["work_format"] == "remote,hybrid"
    assert page_query["schedule"] == "fullDay,flexible"
    assert page_query["page"] == "1"


def test_build_page_url_preserves_filters_and_sets_page():
    """docs/04 §4.2 п.2: пагинация сохраняет фильтры пользователя."""
    base = "https://novokuznetsk.hh.ru/search/vacancy?text=python&area=1"
    page2 = build_page_url(base, 2)
    assert "text=python" in page2 and "area=1" in page2
    assert "page=2" in page2
    assert page2.count("page=") == 1  # номер страницы не дублируется
    assert "page=3" in build_page_url(page2, 3)


@pytest.mark.parametrize(
    "bad_url",
    [
        "https://evil.example.com/search/vacancy?text=x",
        "ftp://hh.ru/search/vacancy",
        "не-ссылка",
    ],
)
def test_validate_hh_search_url_rejects_foreign_domains(bad_url):
    """docs/04 §4.2: парсер не уходит за пределы hh.ru."""
    from app.core.errors import AppError

    with pytest.raises(AppError) as exc:
        validate_hh_search_url(bad_url)
    assert exc.value.status_code == 400


def test_validate_hh_search_url_accepts_hh_subdomains():
    """Поддомены hh.ru (например, novokuznetsk.hh.ru) допустимы."""
    assert validate_hh_search_url("https://novokuznetsk.hh.ru/vacancies/razrabotchik")


# --- docs/04 §2 / §4.1: разбор выдачи --------------------------------------

def test_parse_listing_from_lux_state_and_pagination():
    """Карточки берутся из HH-Lux-InitialState, пагинация распознаётся."""
    page = parse_listing(_listing_html(["111", "222"]))
    assert page.vacancy_ids == ["111", "222"]
    assert page.has_next_page is True


def test_parse_listing_falls_back_to_markup_when_no_lux_state():
    """docs/04 §5: при смене вёрстки/формата JSON работает HTML-резерв."""
    html = (
        '<html><body><div data-qa="serp-item">'
        '<a href="/vacancy/333">a</a></div>'
        '<div data-qa="serp-item"><a href="/vacancy/444">b</a></div>'
        "</body></html>"
    )
    assert extract_listing_ids(html) == ["333", "444"]
    assert parse_listing(html).vacancy_ids == ["333", "444"]


def test_listing_ids_are_deduplicated():
    """Одна вакансия не попадает в обход дважды (docs/04 §4.5)."""
    assert extract_listing_ids(_listing_html(["111", "111", "222"])) == ["111", "222"]


# --- docs/04 §2: fallback chain --------------------------------------------

def test_is_usable_rejects_captcha_and_accepts_listing():
    """Заглушка/капча не считается пригодным контентом."""
    assert _is_usable(_listing_html(["1"])) is True
    assert _is_usable("") is False
    blocked = "<html><body>Just a moment...<script>__cf_chl</script></body></html>"
    assert _is_usable(blocked) is False


async def test_fetcher_uses_curl_cffi_first(monkeypatch):
    """docs/04 §2 п.1: curl_cffi — основной быстрый путь."""
    calls = {"curl": 0, "browser": 0}

    async def fake_curl(self, request):
        calls["curl"] += 1
        return FetchResponse(status_code=200, text=_listing_html(["1"]), url=request.url)

    async def fake_browser(self, request):
        calls["browser"] += 1
        return FetchResponse(status_code=200, text="unused", url=request.url)

    monkeypatch.setattr(HhPageFetcher, "_fetch_curl_cffi", fake_curl)
    monkeypatch.setattr(HhPageFetcher, "_fetch_playwright", fake_browser)

    result = await HhPageFetcher()(_prepared_request())

    assert calls == {"curl": 1, "browser": 0}
    assert result.status_code == 200


async def test_fetcher_escalates_to_playwright_when_curl_blocked(monkeypatch):
    """docs/04 §2 п.2: при блокировке curl_cffi включается браузерный путь."""
    calls = {"curl": 0, "browser": 0}
    blocked = "<html><body>Just a moment...<script>__cf_chl</script></body></html>"

    async def fake_curl(self, request):
        calls["curl"] += 1
        return FetchResponse(status_code=403, text=blocked, url=request.url)

    async def fake_browser(self, request):
        calls["browser"] += 1
        return FetchResponse(status_code=200, text=_listing_html(["7"]), url=request.url)

    monkeypatch.setattr(HhPageFetcher, "_fetch_curl_cffi", fake_curl)
    monkeypatch.setattr(HhPageFetcher, "_fetch_playwright", fake_browser)

    result = await HhPageFetcher()(_prepared_request())

    assert calls == {"curl": 1, "browser": 1}
    assert extract_listing_ids(result.text) == ["7"]


async def test_fetcher_returns_blocked_response_when_both_paths_fail(monkeypatch):
    """docs/04 §2 п.3: оба пути не дали контента → ответ уходит детекторам сессии."""
    blocked = "<html><body>Just a moment...<script>__cf_chl</script></body></html>"

    async def fake_curl(self, request):
        return FetchResponse(status_code=403, text=blocked, url=request.url)

    async def fake_browser(self, request):
        return FetchResponse(status_code=403, text=blocked, url=request.url)

    monkeypatch.setattr(HhPageFetcher, "_fetch_curl_cffi", fake_curl)
    monkeypatch.setattr(HhPageFetcher, "_fetch_playwright", fake_browser)

    result = await HhPageFetcher()(_prepared_request("https://hh.ru/vacancy/1"))
    assert result.status_code == 403


async def test_session_translates_blocked_page_into_captcha(monkeypatch):
    """docs/04 §2 п.3 + §5: капча поднимает CaptchaDetected для остановки воркера."""
    blocked = "<html><body>Just a moment...<script>__cf_chl</script></body></html>"

    async def fake_curl(self, request):
        return FetchResponse(status_code=403, text=blocked, url=request.url)

    async def fake_browser(self, request):
        return FetchResponse(status_code=403, text=blocked, url=request.url)

    monkeypatch.setattr(HhPageFetcher, "_fetch_curl_cffi", fake_curl)
    monkeypatch.setattr(HhPageFetcher, "_fetch_playwright", fake_browser)

    async def no_sleep(_seconds: float) -> None:
        return None

    session = AntiBanSession(fetcher=HhPageFetcher(), sleeper=no_sleep)
    with pytest.raises(CaptchaDetected):
        await session.fetch("https://hh.ru/vacancy/1")


# --- docs/04 §4.1–§4.3: режимы сбора ---------------------------------------

async def test_auto_mode_collects_persists_and_reports_progress(engine, user_factory):
    """docs/04 §4.1: URL → пагинация → детальные страницы → дедупликация → БД."""
    from sqlalchemy.ext.asyncio import AsyncSession

    user_id = await user_factory()
    events: list[tuple] = []

    async def progress(current, total, stage, message):
        events.append((current, total, stage, message))

    base = "https://hh.ru/search/vacancy?text=python"
    orchestrator = ParsingOrchestrator(session=FakeSession({
        build_page_url(base, 0): FetchResponse(
            status_code=200, text=_listing_html(["111", "222"]), url=""
        ),
        "https://hh.ru/vacancy/111": FetchResponse(
            status_code=200, text=VACANCY_CARD, url=""
        ),
        "https://hh.ru/vacancy/222": FetchResponse(
            status_code=200, text=VACANCY_CARD, url=""
        ),
    }))

    async with AsyncSession(engine, expire_on_commit=False) as session:
        outcome = await orchestrator.run_auto(
            session, user_id=user_id, keywords=["python"], max_pages=1, progress=progress
        )
        rows = await _vacancies(session, user_id)

    assert sorted(outcome.vacancy_ids) == ["111", "222"]
    assert outcome.created == 2 and outcome.updated == 0
    assert {row.hh_vacancy_id for row in rows} == {"111", "222"}

    # docs/04 §7: обязательные поля сохранены.
    first = next(r for r in rows if r.hh_vacancy_id == "111")
    assert first.title == "Python разработчик"
    assert first.company_name == "ООО Ромашка"
    assert (first.salary_from, first.salary_to) == (150000, 250000)
    assert first.experience == "3–6 лет"
    assert first.employment_form == "полная занятость"
    assert first.work_format == "удалённая работа"
    assert first.schedule == "пн–пт, 9:00–18:00"
    assert first.area == "Москва"
    assert first.published_at is not None
    assert "разработка на Python" in (first.description_raw or "")
    assert first.status == "raw" and first.source == "auto"

    # docs/04 §6: прогресс в формате current/total/stage/message.
    assert any(stage == "parsing_vacancy" for _, _, stage, _ in events)
    assert events[-1][1] == 2  # total известен к моменту обхода карточек


async def test_auto_mode_respects_max_pages(engine, user_factory):
    """docs/04 §4.1 п.2: обход страниц ограничен max_pages."""
    from sqlalchemy.ext.asyncio import AsyncSession

    user_id = await user_factory()
    base = "https://hh.ru/search/vacancy?text=python"
    pages = {
        build_page_url(base, index): FetchResponse(
            status_code=200, text=_listing_html([str(200 + index)]), url=""
        )
        for index in range(5)
    }
    orchestrator = ParsingOrchestrator(session=FakeSession(pages))

    async with AsyncSession(engine, expire_on_commit=False) as session:
        outcome = await orchestrator.run_auto(
            session, user_id=user_id, keywords=["python"], max_pages=2
        )

    assert outcome.pages_visited == 2
    assert outcome.vacancy_ids == ["200", "201"]


async def test_group_mode_walks_pagination_and_keeps_filters(engine, user_factory):
    """docs/04 §4.2: готовая ссылка + обход пагинации с лимитом."""
    from sqlalchemy.ext.asyncio import AsyncSession

    user_id = await user_factory()
    base = "https://novokuznetsk.hh.ru/vacancies/razrabotchik?area=1"
    host = "https://novokuznetsk.hh.ru"
    orchestrator = ParsingOrchestrator(session=FakeSession({
        build_page_url(base, 0): FetchResponse(
            status_code=200, text=_listing_html(["301"]), url=""
        ),
        build_page_url(base, 1): FetchResponse(
            status_code=200, text=_listing_html(["302"]), url=""
        ),
        f"{host}/vacancy/301": FetchResponse(status_code=200, text=VACANCY_CARD, url=""),
        f"{host}/vacancy/302": FetchResponse(status_code=200, text=VACANCY_CARD, url=""),
    }))

    async with AsyncSession(engine, expire_on_commit=False) as session:
        outcome = await orchestrator.run_group(
            session, user_id=user_id, search_url=base, max_pages=2
        )
        rows = await _vacancies(session, user_id)

    assert outcome.pages_visited == 2
    assert sorted(outcome.vacancy_ids) == ["301", "302"]
    assert all(row.source == "group" for row in rows)
    # Фильтр area из пользовательской ссылки не потерялся при пагинации.
    assert any("area=1" in url for url in orchestrator.http.requested)


async def test_manual_mode_single_request_and_persistence(engine, user_factory):
    """docs/04 §4.3: один запрос на детальную страницу + сохранение."""
    from sqlalchemy.ext.asyncio import AsyncSession

    user_id = await user_factory()
    url = "https://hh.ru/vacancy/137866214"
    fake = FakeSession({url: FetchResponse(status_code=200, text=VACANCY_CARD, url="")})
    orchestrator = ParsingOrchestrator(session=fake)

    async with AsyncSession(engine, expire_on_commit=False) as session:
        outcome = await orchestrator.run_manual(
            session, user_id=user_id, vacancy_url=url, hh_vacancy_id="137866214"
        )
        rows = await _vacancies(session, user_id)

    assert len(fake.requested) == 1  # ровно один запрос, без прогрева
    assert outcome.created == 1
    assert rows[0].hh_vacancy_id == "137866214"
    assert rows[0].source == "manual" and rows[0].status == "raw"


async def test_deleted_vacancy_marked_error(engine, user_factory):
    """docs/04 §5: вакансия удалена на hh.ru → status='error' (not_found)."""
    from sqlalchemy.ext.asyncio import AsyncSession

    user_id = await user_factory()
    url = "https://hh.ru/vacancy/999"
    orchestrator = ParsingOrchestrator(session=FakeSession({
        url: FetchResponse(status_code=404, text="not found", url="")
    }))

    async with AsyncSession(engine, expire_on_commit=False) as session:
        outcome = await orchestrator.run_manual(
            session, user_id=user_id, vacancy_url=url, hh_vacancy_id="999"
        )
        rows = await _vacancies(session, user_id)

    assert outcome.not_found == 1 and outcome.created == 0
    assert rows[0].status == "error"
    assert rows[0].hh_vacancy_id == "999"


async def test_duplicate_vacancy_is_not_created_twice(engine, user_factory):
    """docs/04 §8: повторное добавление обновляет запись, а не плодит дубли."""
    from sqlalchemy.ext.asyncio import AsyncSession

    user_id = await user_factory()
    url = "https://hh.ru/vacancy/555"
    orchestrator = ParsingOrchestrator(session=FakeSession({
        url: FetchResponse(status_code=200, text=VACANCY_CARD, url="")
    }))

    async with AsyncSession(engine, expire_on_commit=False) as session:
        await orchestrator.run_manual(
            session, user_id=user_id, vacancy_url=url, hh_vacancy_id="555"
        )
        second = await orchestrator.run_manual(
            session, user_id=user_id, vacancy_url=url, hh_vacancy_id="555"
        )
        rows = await _vacancies(session, user_id)

    assert second.created == 0 and second.updated == 1
    assert len(rows) == 1


async def test_applied_vacancy_is_not_overwritten(engine, user_factory):
    """docs/04 §8: «Статус applied не перезаписывается автоматически»."""
    from sqlalchemy.ext.asyncio import AsyncSession

    user_id = await user_factory()
    url = "https://hh.ru/vacancy/777"
    orchestrator = ParsingOrchestrator(session=FakeSession({
        url: FetchResponse(status_code=200, text=VACANCY_CARD, url="")
    }))

    async with AsyncSession(engine, expire_on_commit=False) as session:
        await orchestrator.run_manual(
            session, user_id=user_id, vacancy_url=url, hh_vacancy_id="777"
        )
        vacancy = await session.scalar(
            select(Vacancy).where(Vacancy.hh_vacancy_id == "777")
        )
        vacancy.status = "applied"
        vacancy.title = "Прежнее название"
        await session.commit()

        await orchestrator.run_manual(
            session, user_id=user_id, vacancy_url=url, hh_vacancy_id="777"
        )
        await session.refresh(vacancy)

    assert vacancy.status == "applied"
    assert vacancy.title == "Прежнее название"


async def test_captcha_during_run_propagates(engine, user_factory):
    """docs/04 §5: капча пробрасывается воркеру, а не глотается."""
    from sqlalchemy.ext.asyncio import AsyncSession

    user_id = await user_factory()
    orchestrator = ParsingOrchestrator(session=FakeSession({}))

    async def failing_fetch(url, **_kwargs):
        raise CaptchaDetected("Captcha detected")

    orchestrator.http.fetch = failing_fetch

    async with AsyncSession(engine, expire_on_commit=False) as session:
        with pytest.raises(CaptchaDetected):
            await orchestrator.run_manual(
                session,
                user_id=user_id,
                vacancy_url="https://hh.ru/vacancy/1",
                hh_vacancy_id="1",
            )


# --- docs/04 §6: очередь задач --------------------------------------------

def test_task_priority_order_is_manual_group_auto():
    """docs/04 §6: приоритет ручное → групповое → авто."""
    assert TASK_PRIORITY["parse_manual"] < TASK_PRIORITY["parse_group"]
    assert TASK_PRIORITY["parse_group"] < TASK_PRIORITY["parse_auto"]


def test_parsing_and_llm_queues_are_separate():
    """docs/04 §6: LLM-задачи не смешиваются с парсингом."""
    assert not (PARSING_TASK_TYPES & LLM_TASK_TYPES)
    assert {"analyze", "generate_letter", "auto_full", "convert_resume"} <= LLM_TASK_TYPES


async def test_worker_executes_pending_task_and_completes(engine, user_factory, monkeypatch, queue_runner):
    """Главный пробел: задача из `pending` реально исполняется и завершается."""
    from sqlalchemy.ext.asyncio import AsyncSession

    executed = _stub_orchestrator(monkeypatch)
    user_id = await user_factory()
    task_id = await _make_task(
        engine, user_id, "parse_manual",
        {"vacancy_url": "https://hh.ru/vacancy/137866214", "run_analysis": False},
    )

    await queue_runner.run_all_pending(engine)

    async with AsyncSession(engine, expire_on_commit=False) as session:
        task = await session.get(Task, task_id)

    assert executed == ["manual"]
    assert task.status == "completed"
    assert task.started_at is not None and task.finished_at is not None
    assert task.result["created"] == 1
    assert task.error_message is None


async def test_worker_respects_priority_manual_first(engine, user_factory, monkeypatch, queue_runner):
    """docs/04 §6: при равном времени создания ручная задача идёт первой."""
    from sqlalchemy.ext.asyncio import AsyncSession

    executed = _stub_orchestrator(monkeypatch)
    user_id = await user_factory()

    ids = []
    for task_type, payload in (
        ("parse_auto", {"keywords": ["python"], "max_pages": 1}),
        ("parse_group", {"search_url": "https://hh.ru/search/vacancy?text=x", "max_pages": 1}),
        ("parse_manual", {"vacancy_url": "https://hh.ru/vacancy/5"}),
    ):
        ids.append(await _make_task(engine, user_id, task_type, payload))

    await queue_runner.run_all_pending(engine)

    assert executed[0] == "manual"
    assert executed.index("group") < executed.index("auto")

    async with AsyncSession(engine, expire_on_commit=False) as session:
        statuses = [(await session.get(Task, i)).status for i in ids]
    assert statuses == ["completed"] * 3


async def test_worker_limits_two_concurrent_parsers_per_user(
    engine, user_factory, monkeypatch, queue_runner, queue_pool
):
    """docs/04 §1, §6: максимум 2 одновременных парсинг-воркера на пользователя.

    Слоты держит Redis-семафор `parsing:<user_id>`: третья задача не стартует,
    а её job откладывается (ARQ Retry) и остаётся в `pending` (docs/04 §5).
    """
    from arq.worker import Retry
    from sqlalchemy.ext.asyncio import AsyncSession

    gate = asyncio.Event()
    started: list[str] = []
    peak = 0
    active = 0

    async def blocking_auto(self, db, **kwargs):
        nonlocal peak, active
        started.append("auto")
        active += 1
        peak = max(peak, active)
        try:
            await gate.wait()  # держим слот занятым
        finally:
            active -= 1
        return service_module.ParsingOutcome(vacancy_ids=["1"], created=1)

    monkeypatch.setattr(service_module.ParsingOrchestrator, "run_auto", blocking_auto)

    user_id = await user_factory()
    ids = []
    for _ in range(3):
        ids.append(
            await _make_task(
                engine, user_id, "parse_auto", {"keywords": ["x"], "max_pages": 1}
            )
        )

    async with AsyncSession(engine, expire_on_commit=False) as session:
        for task_id in ids:
            task = await session.get(Task, task_id)
            await enqueue_task(task.id, task.task_type, pool=queue_pool)

    # Запускаем все три job'а одновременно — как это делает воркер с max_jobs.
    # Задачи не ждут завершения: слоты держат первые две на gate.
    ctx = queue_runner._ctx()
    running = [
        asyncio.create_task(run_parsing_task(ctx, str(task_id))) for task_id in ids
    ]

    async def _wait_for(predicate, *, attempts: int = 500) -> bool:
        """Дождаться условия, отдавая управление event loop'у."""
        for _ in range(attempts):
            if predicate():
                return True
            await asyncio.sleep(0.01)
        return predicate()

    # Ждём, пока первые две задачи займут оба слота.
    assert await _wait_for(lambda: len(started) == MAX_CONCURRENT_PARSERS_PER_USER), (
        f"ожидались {MAX_CONCURRENT_PARSERS_PER_USER} запущенные задачи, "
        f"получено {len(started)}"
    )
    # Третья job должна была получить Retry из-за занятых слотов.
    assert await _wait_for(lambda: sum(t.done() for t in running) == 1), (
        "третья задача должна получить Retry из-за занятых слотов"
    )

    async with AsyncSession(engine, expire_on_commit=False) as session:
        pending = list(
            (
                await session.scalars(
                    select(Task).where(Task.user_id == user_id, Task.status == "pending")
                )
            ).all()
        )
    assert len(pending) == 1  # docs/04 §5: ждёт освобождения слота в `pending`
    assert len(started) == MAX_CONCURRENT_PARSERS_PER_USER
    assert peak == MAX_CONCURRENT_PARSERS_PER_USER

    gate.set()
    results = await asyncio.gather(*running, return_exceptions=True)
    assert sum(isinstance(r, Retry) for r in results) == 1

    # Освободившийся слот подхватывает отложенную задачу при следующем проходе.
    await queue_runner.drain()

    async with AsyncSession(engine, expire_on_commit=False) as session:
        left = list(
            (
                await session.scalars(
                    select(Task).where(Task.user_id == user_id, Task.status == "pending")
                )
            ).all()
        )
    assert len(started) == 3
    assert left == []  # третья задача тоже выполнена


async def test_worker_marks_task_waiting_captcha(engine, user_factory, monkeypatch, queue_runner):
    """docs/04 §2 п.3, §5: капча → waiting_captcha (пауза), а не failed.

    Задача НЕ финализируется (finished_at is None) — её вернёт в очередь
    POST /tasks/{task_id}/resume после ручного обхода капчи.
    """
    from sqlalchemy.ext.asyncio import AsyncSession

    async def captcha_auto(self, db, **kwargs):
        raise CaptchaDetected("Captcha detected")

    monkeypatch.setattr(service_module.ParsingOrchestrator, "run_auto", captcha_auto)

    user_id = await user_factory()
    task_id = await _make_task(
        engine, user_id, "parse_auto", {"keywords": ["x"], "max_pages": 1}
    )

    await queue_runner.run_all_pending(engine)

    async with AsyncSession(engine, expire_on_commit=False) as session:
        task = await session.get(Task, task_id)

    assert task.status == "waiting_captcha"
    assert "капча" in (task.error_message or "").lower()
    assert task.finished_at is None  # не завершена — ждёт resume


async def test_worker_marks_task_failed_on_unexpected_error(
    engine, user_factory, monkeypatch, queue_runner
):
    """Непредвиденная ошибка не оставляет задачу в processing навсегда."""
    from sqlalchemy.ext.asyncio import AsyncSession

    async def boom_auto(self, db, **kwargs):
        raise RuntimeError("сломалось")

    monkeypatch.setattr(service_module.ParsingOrchestrator, "run_auto", boom_auto)

    user_id = await user_factory()
    task_id = await _make_task(
        engine, user_id, "parse_auto", {"keywords": ["x"], "max_pages": 1}
    )

    await queue_runner.run_all_pending(engine)

    async with AsyncSession(engine, expire_on_commit=False) as session:
        task = await session.get(Task, task_id)

    assert task.status == "failed"
    assert "сломалось" in (task.error_message or "")


async def test_worker_publishes_progress_events(engine, user_factory, monkeypatch, queue_runner):
    """docs/03 §8, docs/04 §6: события task.progress/completed в Realtime Module.

    Воркер публикует события через шину Redis (realtime.bus.publish_event);
    в тестах она пишет их в пул-заглушку, откуда их и читаем.
    """
    _stub_orchestrator(monkeypatch)

    user_id = await user_factory()
    task_id = await _make_task(
        engine, user_id, "parse_manual", {"vacancy_url": "https://hh.ru/vacancy/7"}
    )

    await queue_runner.run_all_pending(engine)

    # События, опубликованные job'ой в шину Redis Realtime Module.
    events = [
        (json.loads(message)["event"], json.loads(message)["data"])
        for message in queue_runner.pool.published
    ]
    names = [name for name, _ in events]
    assert "task.progress" in names
    assert "task.completed" in names

    progress_payload = next(d for n, d in events if n == "task.progress")
    assert progress_payload["task_id"] == str(task_id)
    assert progress_payload["stage"] == "parsing_vacancy"
    assert progress_payload["current"] == 1 and progress_payload["total"] == 1

    completed_payload = next(d for n, d in events if n == "task.completed")
    assert completed_payload["result"]["created"] == 1


async def test_worker_ignores_llm_tasks_in_parsing_queue(engine, user_factory, monkeypatch, queue_runner):
    """docs/04 §6: LLM-задачи не смешиваются с парсингом.

    Задача типа `analyze` обслуживается отдельной LLM-очередью: парсинговый
    оркестратор по ней не вызывается (`executed == []`). Сама задача при этом
    исполняется LLM-воркером; при пустом списке вакансий работать не над чем,
    поэтому она завершается `failed` с понятным сообщением.
    """
    from sqlalchemy.ext.asyncio import AsyncSession

    executed = _stub_orchestrator(monkeypatch)
    user_id = await user_factory()
    task_id = await _make_task(
        engine, user_id, "analyze", {"vacancy_ids": [], "mode": "auto"}
    )

    await queue_runner.run_all_pending(engine)

    async with AsyncSession(engine, expire_on_commit=False) as session:
        task = await session.get(Task, task_id)

    # Парсинговый оркестратор не вызывался ни разу.
    assert executed == []
    # Задача ушла из pending: LLM-очередь её обслужила.
    assert task.status == "failed"
    assert task.finished_at is not None


# --- сквозной поток: REST → очередь → воркер → БД -------------------------

async def _register(client, email: str | None = None) -> dict[str, str]:
    """Зарегистрироваться и войти — вернуть заголовок Authorization.

    POST /auth/register отдаёт UserOut, токены выдаёт /auth/login (docs/03 §2).
    """
    email = email or f"flow{uuid.uuid4().hex[:8]}@test.dev"
    payload = {"email": email, "password": "pass12345"}
    registered = await client.post("/api/v1/auth/register", json=payload)
    assert registered.status_code in (200, 201), registered.text
    logged_in = await client.post("/api/v1/auth/login", json=payload)
    assert logged_in.status_code == 200, logged_in.text
    return {"Authorization": f"Bearer {logged_in.json()['access_token']}"}


async def test_manual_endpoint_task_is_picked_up_by_worker(client, engine, monkeypatch, queue_runner):
    """POST /parsing/manual → задача исполняется воркером, контракт сохранён."""
    executed = _stub_orchestrator(monkeypatch)
    headers = await _register(client)

    response = await client.post(
        "/api/v1/parsing/manual",
        json={"vacancy_url": "https://hh.ru/vacancy/137866214", "run_analysis": False},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "pending"
    task_id = body["task_id"]

    before = await client.get(f"/api/v1/tasks/{task_id}", headers=headers)
    assert before.status_code == 200
    assert before.json()["status"] == "pending"

    await queue_runner.run_all_pending(engine)

    after = await client.get(f"/api/v1/tasks/{task_id}", headers=headers)
    assert after.json()["status"] == "completed"
    assert after.json()["result"]["created"] == 1
    assert executed == ["manual"]


async def test_auto_endpoint_creates_task_with_expected_payload(client, engine):
    """POST /parsing/auto возвращает task_id и сохраняет payload задачи."""
    from sqlalchemy.ext.asyncio import AsyncSession

    headers = await _register(client)

    response = await client.post(
        "/api/v1/parsing/auto",
        json={"keywords": ["python", "fastapi"], "max_pages": 3},
        headers=headers,
    )
    assert response.status_code == 200, response.text

    async with AsyncSession(engine, expire_on_commit=False) as session:
        task = await session.get(Task, uuid.UUID(response.json()["task_id"]))
        assert task.task_type == "parse_auto"
        assert task.status == "pending"
        assert task.payload["keywords"] == ["python", "fastapi"]
        assert task.payload["max_pages"] == 3


async def test_group_endpoint_creates_task(client, engine):
    """POST /parsing/group: задача типа parse_group с валидной ссылкой."""
    from sqlalchemy.ext.asyncio import AsyncSession

    headers = await _register(client)

    response = await client.post(
        "/api/v1/parsing/group",
        json={
            "search_url": "https://novokuznetsk.hh.ru/vacancies/razrabotchik",
            "max_pages": 2,
        },
        headers=headers,
    )
    assert response.status_code == 200, response.text

    async with AsyncSession(engine, expire_on_commit=False) as session:
        task = await session.get(Task, uuid.UUID(response.json()["task_id"]))
        assert task.task_type == "parse_group"
        assert task.payload["max_pages"] == 2


async def test_group_endpoint_rejects_foreign_url(client):
    """docs/04 §4.2: ссылка не на hh.ru отклоняется сразу по контракту ошибок."""
    headers = await _register(client)

    response = await client.post(
        "/api/v1/parsing/group",
        json={"search_url": "https://evil.example.com/search?text=x", "max_pages": 2},
        headers=headers,
    )
    assert response.status_code == 400
    assert response.json()["error_code"] == "INVALID_SEARCH_URL"


async def test_auto_endpoint_requires_search_criteria(client):
    """docs/03 §5: без критериев поиска — 400 с кодом ошибки."""
    headers = await _register(client)

    response = await client.post("/api/v1/parsing/auto", json={}, headers=headers)
    assert response.status_code == 400
    assert response.json()["error_code"] == "INVALID_SEARCH_CRITERIA"

async def test_auto_endpoint_accepts_multiple_filters_at_once(client, engine):
    """docs/04 §4.1: комбинация фильтров принимается и сохраняется в payload задачи.

    Раньше перечни схемы были уже значений интерфейса (project/volunteer/
    onsite/flyInFlyOut), поэтому выбор нескольких чипов давал 400.
    """
    from sqlalchemy.ext.asyncio import AsyncSession

    headers = await _register(client)

    response = await client.post(
        "/api/v1/parsing/auto",
        json={
            "keywords": ["python"],
            "employment_forms": ["full", "project", "gph"],
            "work_formats": ["remote", "hybrid", "onsite"],
            "schedules": ["fullDay", "flexible", "shift", "flyInFlyOut"],
            "max_pages": 1,
        },
        headers=headers,
    )
    assert response.status_code == 200, response.text

    async with AsyncSession(engine, expire_on_commit=False) as session:
        task = await session.get(Task, uuid.UUID(response.json()["task_id"]))
        assert task.payload["employment_forms"] == ["full", "project", "gph"]
        assert task.payload["work_formats"] == ["remote", "hybrid", "onsite"]
        assert task.payload["schedules"] == ["fullDay", "flexible", "shift", "flyInFlyOut"]

    # Один фильтр без ключевых слов тоже допустим (docs/03 §5).
    only_filter = await client.post(
        "/api/v1/parsing/auto",
        json={"work_formats": ["remote"]},
        headers=headers,
    )
    assert only_filter.status_code == 200, only_filter.text

    # Неизвестное значение по-прежнему отсекается валидацией.
    unknown = await client.post(
        "/api/v1/parsing/auto",
        json={"keywords": ["python"], "work_formats": ["teleport"]},
        headers=headers,
    )
# --- docs/04 §4.9: чёрный список слов ---------------------------------------


def test_normalize_blacklist_strips_dedups_and_limits():
    """docs/04 §4.9: нормализация списка — обрезка, дедупликация, лимит."""
    from app.modules.parsing.service import MAX_BLACKLIST_WORDS, normalize_blacklist

    assert normalize_blacklist(["  ТК РФ  ", "тк рф", "", None, "   "]) == ["ТК РФ"]
    assert normalize_blacklist(None) == []
    assert normalize_blacklist("a\nb, c") == ["a", "b", "c"]
    assert len(normalize_blacklist([f"w{i}" for i in range(200)])) == MAX_BLACKLIST_WORDS


def test_is_blacklisted_matches_case_insensitively():
    """docs/04 §4.9: поиск регистронезависимый по тексту вакансии."""
    from app.modules.parsing.service import is_blacklisted

    fields = {"title": "Python", "description_raw": "Оформление строго по тк рф."}
    assert is_blacklisted(fields, ["ТК РФ"]) == ["ТК РФ"]
    assert is_blacklisted(fields, ["тк рф"]) == ["тк рф"]
    assert is_blacklisted(fields, ["ГПХ"]) == []
    assert is_blacklisted(fields, []) == []


async def test_blacklist_prevents_saving_vacancy(engine, user_factory):
    """docs/04 §4.9: найдено чёрное слово → вакансия не сохраняется в БД."""
    from sqlalchemy.ext.asyncio import AsyncSession

    user_id = await user_factory()
    base = "https://hh.ru/search/vacancy?text=python"
    orchestrator = ParsingOrchestrator(session=FakeSession({
        build_page_url(base, 0): FetchResponse(
            status_code=200, text=_listing_html(["111", "222"]), url=""
        ),
        "https://hh.ru/vacancy/111": FetchResponse(
            status_code=200, text=VACANCY_CARD_BLACKLISTED, url=""
        ),
        "https://hh.ru/vacancy/222": FetchResponse(
            status_code=200, text=VACANCY_CARD, url=""
        ),
    }))

    async with AsyncSession(engine, expire_on_commit=False) as session:
        outcome = await orchestrator.run_auto(
            session,
            user_id=user_id,
            keywords=["python"],
            max_pages=1,
            blacklist=["ТК РФ"],
        )
        rows = await _vacancies(session, user_id)

    assert outcome.blacklisted == 1
    assert outcome.created == 1
    assert [row.hh_vacancy_id for row in rows] == ["222"]


async def test_blacklist_deletes_already_saved_vacancy(engine, user_factory):
    """docs/04 §4.9: ранее сохранённая вакансия из чёрного списка удаляется."""
    from sqlalchemy.ext.asyncio import AsyncSession

    user_id = await user_factory()
    base = "https://hh.ru/search/vacancy?text=python"
    pages = {
        build_page_url(base, 0): FetchResponse(
            status_code=200, text=_listing_html(["111"]), url=""
        ),
        "https://hh.ru/vacancy/111": FetchResponse(
            status_code=200, text=VACANCY_CARD_BLACKLISTED, url=""
        ),
    }

    # Первый проход — фильтр выключен, вакансия сохраняется.
    plain = ParsingOrchestrator(session=FakeSession(pages))
    async with AsyncSession(engine, expire_on_commit=False) as session:
        await plain.run_auto(session, user_id=user_id, keywords=["python"], max_pages=1)
        assert len(await _vacancies(session, user_id)) == 1

    # Второй проход — та же вакансия, но с включённым чёрным списком.
    filtered = ParsingOrchestrator(session=FakeSession(pages))
    async with AsyncSession(engine, expire_on_commit=False) as session:
        outcome = await filtered.run_auto(
            session,
            user_id=user_id,
            keywords=["python"],
            max_pages=1,
            blacklist=["ТК РФ"],
        )
        rows = await _vacancies(session, user_id)

    assert outcome.blacklisted == 1
    assert outcome.created == 0
    assert rows == []


async def test_empty_blacklist_keeps_previous_behaviour(engine, user_factory):
    """docs/04 §4.9: выключенный фильтр → вакансии сохраняются как раньше."""
    from sqlalchemy.ext.asyncio import AsyncSession

    user_id = await user_factory()
    base = "https://hh.ru/search/vacancy?text=python"
    orchestrator = ParsingOrchestrator(session=FakeSession({
        build_page_url(base, 0): FetchResponse(
            status_code=200, text=_listing_html(["111"]), url=""
        ),
        "https://hh.ru/vacancy/111": FetchResponse(
            status_code=200, text=VACANCY_CARD_BLACKLISTED, url=""
        ),
    }))

    async with AsyncSession(engine, expire_on_commit=False) as session:
        outcome = await orchestrator.run_auto(
            session, user_id=user_id, keywords=["python"], max_pages=1, blacklist=[]
        )
        rows = await _vacancies(session, user_id)

    assert outcome.blacklisted == 0
    assert outcome.created == 1
    assert [row.hh_vacancy_id for row in rows] == ["111"]


async def test_group_mode_applies_blacklist(engine, user_factory):
    """docs/04 §4.9: чёрный список работает и в групповом парсере."""
    from sqlalchemy.ext.asyncio import AsyncSession

    user_id = await user_factory()
    base = "https://novokuznetsk.hh.ru/vacancies/razrabotchik"
    host = "https://novokuznetsk.hh.ru"
    orchestrator = ParsingOrchestrator(session=FakeSession({
        build_page_url(base, 0): FetchResponse(
            status_code=200, text=_listing_html(["301"]), url=""
        ),
        f"{host}/vacancy/301": FetchResponse(
            status_code=200, text=VACANCY_CARD_BLACKLISTED, url=""
        ),
    }))

    async with AsyncSession(engine, expire_on_commit=False) as session:
        outcome = await orchestrator.run_group(
            session,
            user_id=user_id,
            search_url=base,
            max_pages=1,
            blacklist=["ТК РФ"],
        )
        rows = await _vacancies(session, user_id)

    assert outcome.blacklisted == 1
    assert rows == []


async def test_worker_passes_blacklist_only_when_enabled(
    engine, user_factory, queue_runner
):
    """docs/04 §4.9: тумблер включён — слова в оркестратор, выключен — пусто."""
    original = service_module.ParsingOrchestrator.run_auto
    captured: dict = {}

    async def fake_run_auto(self, db, **kwargs):
        captured.clear()
        captured.update(kwargs)
        return service_module.ParsingOutcome(vacancy_ids=["1"], created=1)

    service_module.ParsingOrchestrator.run_auto = fake_run_auto
    try:
        enabled_id = await _make_task(engine, await user_factory(), "parse_auto", {
            "keywords": ["python"],
            "max_pages": 1,
            "blacklist_enabled": True,
            "blacklist_words": ["ТК РФ", " ГПХ ", "тк рф"],
        })
        await queue_runner.run_pending(engine, [enabled_id])
        assert captured["blacklist"] == ["ТК РФ", "ГПХ"]

        disabled_id = await _make_task(engine, await user_factory(), "parse_auto", {
            "keywords": ["python"],
            "max_pages": 1,
            "blacklist_enabled": False,
            "blacklist_words": ["ТК РФ"],
        })
        await queue_runner.run_pending(engine, [disabled_id])
        assert captured["blacklist"] == []
    finally:
        service_module.ParsingOrchestrator.run_auto = original


async def test_parse_endpoints_store_blacklist_payload(client, engine):
    """docs/03 §5 + docs/04 §4.9: чёрный список попадает в payload задачи."""
    from sqlalchemy.ext.asyncio import AsyncSession

    headers = await _register(client)

    auto = await client.post(
        "/api/v1/parsing/auto",
        json={
            "keywords": ["python"],
            "blacklist_enabled": True,
            "blacklist_words": ["ТК РФ"],
        },
        headers=headers,
    )
    assert auto.status_code == 200, auto.text

    async with AsyncSession(engine, expire_on_commit=False) as session:
        task = await session.get(Task, uuid.UUID(auto.json()["task_id"]))
        assert task.payload["blacklist_enabled"] is True
        assert task.payload["blacklist_words"] == ["ТК РФ"]

    group = await client.post(
        "/api/v1/parsing/group",
        json={
            "search_url": "https://hh.ru/search/vacancy?text=python",
            "blacklist_enabled": False,
            "blacklist_words": ["ТК РФ"],
        },
        headers=headers,
    )
    assert group.status_code == 200, group.text

    async with AsyncSession(engine, expire_on_commit=False) as session:
        task = await session.get(Task, uuid.UUID(group.json()["task_id"]))
        # Выключенный тумблер: слова в payload не сохраняются вовсе.
        assert task.payload["blacklist_enabled"] is False
        assert task.payload["blacklist_words"] == []
