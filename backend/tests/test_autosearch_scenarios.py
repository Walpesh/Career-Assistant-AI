# -*- coding: utf-8 -*-
"""Сценарные тесты Автопоиска (autosearch): фидельность параметров и данных.

Покрывает 5 сценариев (docs/03_API_CONTRACTS.md §5, docs/04_PARSING_RULES.md
§4.1, §10):

    S1: Base Default Autosearch  — без города → поиск по всей России (нет area).
    S2: City-Specific Fidelity   — «Новокузнецк»→area=1240, «Санкт-Петербург»→2,
                                   «Москва»→1; неизвестный город → None + warning
                                   (БЕЗ молчаливого фолбэка на Москву).
    S3: Multi-Source Concurrent   — fan-out оркестратора по SourceRegistry;
                                   персист с корректным source и составной
                                   уникальностью (user_id, source, external_id).
    S4: Advanced Filters          — employment/work_format/schedule/threshold/
                                   max_pages 1:1 в payload и в URL поиска.
    S5: Blacklist Words           — совпавшие вакансии отсеиваются ДО записи в БД.

Фидельность проверяется по всей цепочке:
    UI-ввод → POST /parsing/auto (task.payload) → orchestrator → adapter query
    URL → таблица vacancies (source, external_id, uq user/source/external).

Сеть и браузер не используются: HTTP-эндпоинт только создаёт задачу (enqueue),
поэтому поведение адаптера/оркестратора проверяется напрямую с подменённой
AntiBanSession (FetchResponse) — детерминированно и без обращений к hh.ru.
"""

from __future__ import annotations

import logging
import uuid
from urllib.parse import parse_qsl, urlsplit

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Task, User, Vacancy
from app.modules.anti_ban.session import FetchResponse
from app.modules.parsing.schemas import SUPPORTED_SOURCES
from app.modules.parsing.service import (
    ParsingOrchestrator,
    find_blacklist_matches,
    is_blacklisted,
    normalize_blacklist,
)
from app.modules.parsing.sources.base import BaseSourceAdapter
from app.modules.parsing.sources.hh import HHAdapter
from app.modules.parsing.sources.hh_areas import resolve_city_area
from app.modules.parsing.sources.registry import SourceRegistry
from app.modules.parsing.urls import build_auto_search_url

from conftest import auth_headers_for

API_AUTO = "/api/v1/parsing/auto"


# =====================================================================
# Помощники (сеть/БД без реального фетчинга)
# =====================================================================


class _OkSession:
    """Заглушка AntiBanSession: любой URL отвечает 200 (text = url).

    Оркестратор получает «страницу», текст которой совпадает с URL, поэтому
    ``extract_fields`` видит тот же идентификатор, что и в ссылке.
    """

    def __init__(self) -> None:
        self.requested: list[str] = []

    async def fetch(self, url: str, **_kwargs) -> FetchResponse:
        self.requested.append(url)
        return FetchResponse(status_code=200, text=url, url=url)


def _query(url: str) -> dict[str, str]:
    """Разобрать query-строку URL в словарь."""
    return {key: value for key, value in parse_qsl(urlsplit(url).query)}


async def _make_user(engine) -> User:
    """Пользователь напрямую в тестовой БД (без HTTP-регистрации)."""
    async with AsyncSession(engine, expire_on_commit=False) as session:
        user = User(email=f"auto{uuid.uuid4().hex[:10]}@test.dev", password_hash="x")
        session.add(user)
        await session.commit()
        await session.refresh(user)
        return user


class _FakeAdapter(BaseSourceAdapter):
    """Минимальный адаптер источника для оркестратора (fan-out + персист).

    Реализует только хуки оркестратора autosearch: build_search_url →
    page_url → parse_listing → build_vacancy_url → extract_fields.
    """

    def __init__(self, name: str, ids: list[str]) -> None:
        self._name = name
        self._ids = ids

    @property
    def source_name(self) -> str:
        return self._name

    async def search(self, filters: dict) -> list[dict]:
        return []

    async def get_vacancy(self, external_id_or_url: str) -> dict | None:
        return None

    def normalize(self, raw: dict) -> dict:
        return raw

    def build_search_url(self, **_filters) -> str:
        return f"https://{self._name}.example/search"

    def page_url(self, base_url: str, page_index: int) -> str:
        return base_url if page_index == 0 else f"{base_url}?page={page_index}"

    def parse_listing(self, _text: str) -> tuple[list[str], bool]:
        return list(self._ids), False  # одна страница, без has_next

    def build_vacancy_url(self, base_url: str, external_id: str) -> str:
        return f"https://{self._name}.example/vacancy/{external_id}"

    def extract_fields(self, text: str) -> dict:
        return {
            "title": f"vacancy {text.rsplit('/', 1)[-1]}",
            "company_name": f"{self._name} Co",
            "description_raw": text,
        }


class _BlacklistAdapter(_FakeAdapter):
    """Фейковый адаптер, у которого id с префиксом «bl» содержат чёрные слова."""

    def extract_fields(self, text: str) -> dict:
        external_id = text.rsplit("/", 1)[-1]
        desc = (
            "Оформление по ГПХ, трудовой договор не по ТК РФ"
            if external_id.startswith("bl")
            else f"Чистая вакансия {external_id}: python fastapi"
        )
        return {
            "title": f"vacancy {external_id}",
            "company_name": f"{self._name} Co",
            "description_raw": desc,
        }


# =====================================================================
# S1 — Base Default Autosearch (No City Restriction)
# =====================================================================


async def test_s1_api_default_payload_no_city(client):
    """S1: keywords=[python] без city/sources → task.payload хранит дефолты.

    Верификация: city отсутствует (model_dump exclude_none), sources==['hh'],
    task_type=='parse_auto' (input_mode=auto), дефолты threshold=70/max_pages=5.
    """
    headers = await auth_headers_for(client)
    resp = await client.post(API_AUTO, json={"keywords": ["python"]}, headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "pending"

    task_id = resp.json()["task_id"]
    async with client.test_session_factory() as session:
        task = await session.get(Task, task_id)
        assert task is not None
        assert task.task_type == "parse_auto"  # input_mode = auto
        assert task.payload["keywords"] == ["python"]
        assert task.payload["sources"] == ["hh"]  # дефолт docs/04 §10.2
        # exclude_none=True → пустой город не попадает в payload (omitted/null)
        assert "city" not in task.payload
        assert task.payload.get("match_threshold") == 70
        assert task.payload.get("max_pages") == 5


def test_s1_empty_city_builds_url_without_area():
    """S1: пустой/None город → URL поиска hh БЕЗ параметра area (вся Россия)."""
    adapter = HHAdapter(_OkSession())
    for city in (None, ""):
        url = adapter.build_search_url(keywords=["python"], city=city)
        params = _query(url)
        assert "area" not in params, f"area не должен быть для city={city!r}: {url}"
        assert params["text"] == "python"

    # Прямая сборка URL подтверждает то же поведение.
    assert "area" not in _query(build_auto_search_url(keywords=["python"]))


# =====================================================================
# S2 — City-Specific Search Fidelity Test
# =====================================================================


@pytest.mark.parametrize(
    "city_input,expected_area",
    [
        ("Новокузнецк", 1240),
        ("Санкт-Петербург", 2),
        ("Москва", 1),
        ("  новокузнецк  ", 1240),  # регистр/пробелы нормализуются
    ],
)
def test_s2_city_resolves_to_area(city_input, expected_area):
    """S2: кириллический ввод города корректно маппится в area ID hh."""
    assert resolve_city_area(city_input) == expected_area
    # _resolve_city_area — тот же резолвер, что использует адаптер.
    assert HHAdapter._resolve_city_area(city_input) == expected_area


@pytest.mark.parametrize(
    "city_input,expected_area",
    [
        ("Новокузнецк", 1240),
        ("Санкт-Петербург", 2),
        ("Москва", 1),
    ],
)
def test_s2_area_present_in_search_url(city_input, expected_area):
    """S2: area=<id> явно присутствует в query строке исходящего URL поиска."""
    adapter = HHAdapter(_OkSession())
    url = adapter.build_search_url(keywords=["fastapi"], city=city_input)
    params = _query(url)
    assert params["area"] == str(expected_area)
    assert params["text"] == "fastapi"


def test_s2_unknown_city_no_moscow_fallback(caplog):
    """S2 (negative): неизвестный город → area=None + warning, БЕЗ area в URL.

    Гарантия отсутствия молчаливого фолбэка на Москву (area=1): в URL нет area,
    а в лог уходит явное предупреждение.
    """
    adapter = HHAdapter(_OkSession())
    with caplog.at_level(logging.WARNING):
        area = adapter._resolve_city_area("Атлантида")
        url = adapter.build_search_url(keywords=["x"], city="Атлантида")

    assert area is None
    assert resolve_city_area("Атлантида") is None
    assert "area" not in _query(url), "не должен быть молчаливый фолбэк на area"
    assert any("not recognized" in rec.message for rec in caplog.records), caplog.text


async def test_s2_api_preserves_city_in_payload(client):
    """S2: city из UI 1:1 сохраняется в task.payload для адаптера."""
    headers = await auth_headers_for(client)
    resp = await client.post(
        API_AUTO,
        json={"keywords": ["fastapi"], "city": "Новокузнецк", "sources": ["hh"]},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text

    task_id = resp.json()["task_id"]
    async with client.test_session_factory() as session:
        task = await session.get(Task, task_id)
        assert task is not None
        assert task.payload["city"] == "Новокузнецк"
        assert task.payload["keywords"] == ["fastapi"]
        assert task.payload["sources"] == ["hh"]


# =====================================================================
# S3 — Multi-Source Concurrent Autosearch
# =====================================================================


async def test_s3_api_accepts_all_selected_sources(client):
    """S3: sources=[hh, remoteok, remotive] сохраняются в payload без изменений."""
    headers = await auth_headers_for(client)
    sources = ["hh", "remoteok", "remotive"]
    assert set(sources) <= set(SUPPORTED_SOURCES)

    resp = await client.post(
        API_AUTO,
        json={"keywords": ["backend"], "sources": sources, "city": "Москва"},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text

    task_id = resp.json()["task_id"]
    async with client.test_session_factory() as session:
        task = await session.get(Task, task_id)
        assert task is not None
        assert task.payload["sources"] == sources
        assert task.payload["keywords"] == ["backend"]


async def test_s3_orchestrator_fans_out_and_persists_per_source(engine):
    """S3: оркестратор обходит ВСЕ источники и пишет source каждой вакансии.

    Проверяет fan-out через SourceRegistry и персист с корректным source;
    уникальность (user_id, source, external_id) — в отдельном тесте.
    """
    user = await _make_user(engine)

    registry = SourceRegistry()
    registry.register(_FakeAdapter("hh", ["h1", "h2"]))
    registry.register(_FakeAdapter("remoteok", ["r1", "r2"]))
    registry.register(_FakeAdapter("remotive", ["m1"]))

    http = _OkSession()
    orchestrator = ParsingOrchestrator(
        session=http, sources=["hh", "remoteok", "remotive"], registry=registry
    )

    async with AsyncSession(engine, expire_on_commit=False) as session:
        outcome = await orchestrator.run_auto(
            session, user_id=user.id, keywords=["backend"], max_pages=1
        )
        rows = list(
            (
                await session.scalars(
                    select(Vacancy).where(Vacancy.user_id == user.id)
                )
            ).all()
        )

    by_source: dict[str, list[str]] = {}
    for row in rows:
        by_source.setdefault(row.source, []).append(row.external_id)
    by_source = {src: sorted(ids) for src, ids in by_source.items()}

    assert by_source == {
        "hh": ["h1", "h2"],
        "remoteok": ["r1", "r2"],
        "remotive": ["m1"],
    }
    assert outcome.created == 5
    # Каждый источник получил свой поисковый запрос (fan-out по всем площадкам).
    searched = [u for u in http.requested if u.endswith("/search")]
    assert searched == [
        "https://hh.example/search",
        "https://remoteok.example/search",
        "https://remotive.example/search",
    ]


async def test_s3_composite_uniqueness_user_source_external(engine):
    """S3: повторный автопоиск дедуплицируется по (user_id, source, external_id).

    Второй прогон тех же id не создаёт дублей — created==0, updated==N.
    """
    user = await _make_user(engine)
    registry = SourceRegistry()
    registry.register(_FakeAdapter("hh", ["h1", "h2"]))
    registry.register(_FakeAdapter("remoteok", ["r1"]))

    def build():
        return ParsingOrchestrator(
            session=_OkSession(),
            sources=["hh", "remoteok"],
            registry=registry,
        )

    async with AsyncSession(engine, expire_on_commit=False) as session:
        first = await build().run_auto(
            session, user_id=user.id, keywords=["backend"], max_pages=1
        )
    assert first.created == 3 and first.updated == 0

    async with AsyncSession(engine, expire_on_commit=False) as session:
        second = await build().run_auto(
            session, user_id=user.id, keywords=["backend"], max_pages=1
        )
        total = len(
            list(
                (
                    await session.scalars(
                        select(Vacancy).where(Vacancy.user_id == user.id)
                    )
                ).all()
            )
        )

    assert second.created == 0 and second.updated == 3, second
    assert total == 3  # дублей по (user_id, source, external_id) нет


# =====================================================================
# S4 — Advanced Filters & Thresholds
# =====================================================================


async def test_s4_api_preserves_filters_and_thresholds(client):
    """S4: фильтры/порог/лимит страниц 1:1 сохраняются в task.payload.

    ВАЖНО: API ожидает коды-энумы (full/remote/fullDay), а не русские подписи
    UI — иначе Pydantic Literal отклонит запрос (см. test_s4_rejects_russian_labels).
    """
    headers = await auth_headers_for(client)
    payload = {
        "keywords": ["python"],
        "employment_forms": ["full"],
        "work_formats": ["remote"],
        "schedules": ["fullDay"],
        "match_threshold": 80,
        "max_pages": 2,
        "sources": ["hh"],
    }

    resp = await client.post(API_AUTO, json=payload, headers=headers)
    assert resp.status_code == 200, resp.text

    task_id = resp.json()["task_id"]
    async with client.test_session_factory() as session:
        task = await session.get(Task, task_id)
        assert task is not None
        assert task.payload["employment_forms"] == ["full"]
        assert task.payload["work_formats"] == ["remote"]
        assert task.payload["schedules"] == ["fullDay"]
        assert task.payload["match_threshold"] == 80
        assert task.payload["max_pages"] == 2


def test_s4_filters_serialize_into_search_url():
    """S4: фильтры сериализуются в query параметры URL поиска hh."""
    adapter = HHAdapter(_OkSession())
    url = adapter.build_search_url(
        keywords=["python"],
        employment_forms=["full"],
        work_formats=["remote"],
        schedules=["fullDay"],
    )
    params = _query(url)
    assert params["employment"] == "full"
    assert params["work_format"] == "remote"
    assert params["schedule"] == "fullDay"
    assert params["text"] == "python"

    # Несколько значений в одном фильтре → склейка запятыми (hh.ru).
    multi = _query(
        build_auto_search_url(keywords=["backend"], employment_forms=["full", "part"])
    )
    assert multi["employment"] == "full,part"


async def test_s4_rejects_russian_labels(client):
    """S4 (fidelity): русские подписи фильтров → 400 VALIDATION_ERROR.

    Документирует контракт: UI должен отправлять коды-энумы, иначе запрос
    отклоняется, а не молча теряет фильтр (нет silent-fallback).
    """
    headers = await auth_headers_for(client)
    resp = await client.post(
        API_AUTO,
        json={
            "keywords": ["python"],
            "employment_forms": ["Полная"],
            "work_formats": ["Удалённо"],
            "schedules": ["Полный день"],
        },
        headers=headers,
    )
    assert resp.status_code == 400, resp.text
    assert resp.json()["error_code"] == "VALIDATION_ERROR"


# =====================================================================
# S5 — Blacklist Words Filtering
# =====================================================================


async def test_s5_api_normalizes_blacklist_into_payload(client):
    """S5: blacklist_enabled+words сохраняются в payload (с нормализацией)."""
    headers = await auth_headers_for(client)
    resp = await client.post(
        API_AUTO,
        json={
            "keywords": ["python"],
            "blacklist_enabled": True,
            "blacklist_words": ["ГПХ", "ТК РФ"],
        },
        headers=headers,
    )
    assert resp.status_code == 200, resp.text

    task_id = resp.json()["task_id"]
    async with client.test_session_factory() as session:
        task = await session.get(Task, task_id)
        assert task is not None
        assert task.payload["blacklist_enabled"] is True
        assert task.payload["blacklist_words"] == ["ГПХ", "ТК РФ"]


def test_s5_normalize_and_match_helpers():
    """S5: normalize_blacklist чистит/дедуплицирует; поиск совпадений регистронезависим."""
    assert normalize_blacklist(["ГПХ", "ГПХ", "  ТК РФ  ", "", None]) == [
        "ГПХ",
        "ТК РФ",
    ]
    fields = {"description_raw": "Оформление по ГПХ, не по ТК РФ"}
    assert set(find_blacklist_matches("ГПХ договор", ["гпх", "тк рф"])) == {"гпх"}
    assert set(is_blacklisted(fields, ["гпх", "тк рф"])) == {"гпх", "тк рф"}


async def test_s5_blacklisted_vacancy_not_persisted(engine):
    """S5: вакансия с чёрным словом отсеивается ДО записи в БД и удаляется."""
    user = await _make_user(engine)
    registry = SourceRegistry()
    registry.register(_BlacklistAdapter("hh", ["ok1", "bl1"]))

    orchestrator = ParsingOrchestrator(
        session=_OkSession(), sources=["hh"], registry=registry
    )
    async with AsyncSession(engine, expire_on_commit=False) as session:
        outcome = await orchestrator.run_auto(
            session,
            user_id=user.id,
            keywords=["python"],
            max_pages=1,
            blacklist=["ГПХ", "ТК РФ"],
        )
        stored = {
            row.external_id
            for row in (
                await session.scalars(
                    select(Vacancy).where(Vacancy.user_id == user.id)
                )
            ).all()
        }

    assert outcome.blacklisted == 1
    assert outcome.created == 1  # только чистая ok1 сохранена
    assert stored == {"ok1"}
    assert "bl1" not in stored  # чёрный список отсечён до вставки
