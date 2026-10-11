"""Интеграционные тесты мульти-источникового парсинга (docs/04 §10).

Покрывают:
    - контракт POST /parsing/auto|group: поле ``sources``, значение по
      умолчанию ["hh"], неизвестная площадка → 422 (docs/03 §5, docs/04 §10.2);
    - fallback воркера: пустой/отсутствующий ``sources`` → ["hh"]
      (docs/04 §10.2, ``DEFAULT_SOURCES``);
    - фильтр GET /vacancies?source=... по 8 площадкам (docs/03 §4, docs/04 §10.4);
    - целостность БД: CHECK ``ck_vacancies_source`` и составной уникальный
      индекс ``uq_vacancies_user_source_external`` (docs/02 §3.3);
    - обход НЕСКОЛЬКИХ источников оркестратором с сохранением ``source``
      каждой вакансии (docs/04 §10.2, §10.4).

Сеть и браузер не используются: HTTP-эндпоинты создают задачу (fetch не
вызывается), обход источников проверяется на уровне оркестратора с
подменённой сессией и фейковыми адаптерами источников.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Task, User, Vacancy
from app.modules.anti_ban.session import FetchResponse
from app.modules.parsing.schemas import SUPPORTED_SOURCES
from app.modules.parsing.service import ParsingOrchestrator
from app.modules.parsing.sources import default_registry
from app.modules.parsing.sources.base import BaseSourceAdapter
from app.modules.parsing.sources.registry import SourceRegistry

from conftest import register_verified, user_id_for

API = "/api/v1"
PASSWORD = "strongpassword"


# --- помощники -------------------------------------------------------------


async def _auth(client, email: str) -> dict[str, str]:
    """Регистрация + подтверждение email → заголовок Authorization."""
    tokens = await register_verified(client, email, PASSWORD)
    return {"Authorization": f"Bearer {tokens['access_token']}"}


async def _make_user(engine) -> uuid.UUID:
    """Пользователь напрямую в тестовой БД (без HTTP-регистрации)."""
    async with AsyncSession(engine, expire_on_commit=False) as session:
        user = User(email=f"ms{uuid.uuid4().hex[:10]}@test.dev", password_hash="x")
        session.add(user)
        await session.commit()
        return user.id


async def _make_task(engine, user_id: uuid.UUID, task_type: str, payload: dict) -> uuid.UUID:
    """Pending-задача парсинга в тестовой БД (как в test_parsing_flow)."""
    async with AsyncSession(engine, expire_on_commit=False) as session:
        task = Task(
            user_id=user_id,
            task_type=task_type,
            status="pending",
            progress_current=0,
            progress_total=0,
            payload=payload,
        )
        session.add(task)
        await session.commit()
        await session.refresh(task)
        return task.id


class _OkSession:
    """AntiBanSession-заглушка: любой URL отвечает 200 (text = url)."""

    def __init__(self) -> None:
        self.requested: list[str] = []

    async def fetch(self, url: str, **_kwargs) -> FetchResponse:
        self.requested.append(url)
        return FetchResponse(status_code=200, text=url, url=url)


class _FakeSession:
    """AntiBanSession-заглушка: любой URL отвечает 404 (нет сети)."""

    def __init__(self) -> None:
        self.requested: list[str] = []

    async def fetch(self, url: str, **_kwargs) -> FetchResponse:
        self.requested.append(url)
        return FetchResponse(status_code=404, text="", url=url)


class _FakeAdapter(BaseSourceAdapter):
    """Минимальный адаптер источника для проверки обхода нескольких площадок.

    Реализует только то, что использует оркестратор в режиме автопоиска:
    ``build_search_url`` → ``page_url`` → ``parse_listing`` →
    ``build_vacancy_url`` → ``extract_fields``. Каждый источник отдаёт свой
    фиксированный набор внешних id.
    """

    def __init__(self, name: str, ids: list[str]) -> None:
        self._name = name
        self._ids = ids

    @property
    def source_name(self) -> str:
        return self._name

    # --- абстрактный контракт (в этом сценарии не вызывается) -------------
    async def search(self, filters: dict) -> list[dict]:
        return []

    async def get_vacancy(self, external_id_or_url: str) -> dict | None:
        return None

    def normalize(self, raw: dict) -> dict:
        return raw

    # --- хуки оркестратора (docs/04 §10.2) ---------------------------------
    def build_search_url(self, **_filters) -> str:
        return f"https://{self._name}.example/search"

    def page_url(self, base_url: str, page_index: int) -> str:
        return base_url if page_index == 0 else f"{base_url}?page={page_index}"

    def parse_listing(self, _text: str) -> tuple[list[str], bool]:
        return list(self._ids), False

    def build_vacancy_url(self, base_url: str, external_id: str) -> str:
        return f"https://{self._name}.example/vacancy/{external_id}"

    def extract_fields(self, text: str) -> dict:
        return {"title": text, "company_name": f"{self._name} Co", "description_raw": text}


# ============================================================
# Контракт POST /parsing/auto|group: поле sources (docs/03 §5)
# ============================================================


async def test_parsing_auto_sources_default_to_hh(client, engine):
    """Без ``sources`` в payload задачи попадает значение по умолчанию ["hh"]."""
    headers = await _auth(client, f"auto-def-{uuid.uuid4().hex[:8]}@test.dev")

    resp = await client.post(
        f"{API}/parsing/auto", json={"keywords": ["python"]}, headers=headers
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "pending"

    async with AsyncSession(engine, expire_on_commit=False) as session:
        task = await session.get(Task, uuid.UUID(resp.json()["task_id"]))
    assert task is not None
    assert task.payload["sources"] == ["hh"]


async def test_parsing_auto_accepts_multi_source(client, engine):
    """Несколько площадок принимаются и сохраняются в payload без изменений."""
    headers = await _auth(client, f"auto-multi-{uuid.uuid4().hex[:8]}@test.dev")
    sources = ["hh", "remoteok", "greenhouse"]

    resp = await client.post(
        f"{API}/parsing/auto",
        json={"keywords": ["python"], "sources": sources},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text

    async with AsyncSession(engine, expire_on_commit=False) as session:
        task = await session.get(Task, uuid.UUID(resp.json()["task_id"]))
    assert task.payload["sources"] == sources


async def test_parsing_auto_rejects_unknown_source(client):
    """Неизвестная площадка → 400 VALIDATION_ERROR (валидация Literal).

    Pydantic отклонит значение ``sources`` как не из Literal; глобальный
    обработчик ошибок приложения отдаёт это как 400 (docs/03 §1), а не 422.
    """
    headers = await _auth(client, f"auto-bad-{uuid.uuid4().hex[:8]}@test.dev")

    resp = await client.post(
        f"{API}/parsing/auto",
        json={"keywords": ["python"], "sources": ["unknown"]},
        headers=headers,
    )
    assert resp.status_code == 400, resp.text
    assert resp.json()["error_code"] == "VALIDATION_ERROR"


async def test_parsing_auto_requires_search_criteria(client):
    """Пустой запрос без критериев → 400 INVALID_SEARCH_CRITERIA."""
    headers = await _auth(client, f"auto-empty-{uuid.uuid4().hex[:8]}@test.dev")

    resp = await client.post(f"{API}/parsing/auto", json={}, headers=headers)
    assert resp.status_code == 400, resp.text


async def test_parsing_group_sources_default_to_hh(client, engine):
    """Групповой парсер: без ``sources`` в payload попадает ["hh"]."""
    headers = await _auth(client, f"group-def-{uuid.uuid4().hex[:8]}@test.dev")

    resp = await client.post(
        f"{API}/parsing/group",
        json={"search_url": "https://hh.ru/search/vacancy?text=python"},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text

    async with AsyncSession(engine, expire_on_commit=False) as session:
        task = await session.get(Task, uuid.UUID(resp.json()["task_id"]))
    assert task.payload["sources"] == ["hh"]



# ============================================================
# Fallback воркера: пустой sources → ["hh"] (docs/04 §10.2)
# ============================================================


async def test_worker_empty_sources_falls_back_to_hh(engine, queue_runner):
    """Пустой список источников в payload → оркестратор получает ["hh"]."""
    from app.modules.parsing import service as svc

    user_id = await _make_user(engine)
    session_mock = _FakeSession()
    captured: dict = {}
    original_init = svc.ParsingOrchestrator.__init__

    def patched_init(self, session=None, sources=None, registry=None):
        captured["sources"] = list(sources) if sources is not None else None
        original_init(self, session=session_mock, sources=sources, registry=registry)

    svc.ParsingOrchestrator.__init__ = patched_init
    try:
        task_id = await _make_task(
            engine,
            user_id,
            "parse_auto",
            {"keywords": ["python"], "max_pages": 1, "sources": []},
        )
        await queue_runner.run_pending(engine, [task_id])
    finally:
        svc.ParsingOrchestrator.__init__ = original_init

    assert captured["sources"] == ["hh"]


# ============================================================
# Реестр источников согласован со схемой (docs/04 §10.3)
# ============================================================


def test_default_registry_has_all_supported_sources():
    """Каждая площадка из SUPPORTED_SOURCES зарегистрирована в реестре."""
    names = set(default_registry().names())
    assert set(SUPPORTED_SOURCES) <= names


# ============================================================
# Обход нескольких источников оркестратором (docs/04 §10.2, §10.4)
# ============================================================


async def test_run_auto_collects_from_all_sources(engine):
    """Автопоиск обходит все источники и сохраняет ``source`` каждой вакансии.

    Имена источников — реальные площадки (docs/04 §10.4): значение ``source``
    проверяется CHECK-ограничением БД ``ck_vacancies_source``.
    """
    user_id = await _make_user(engine)

    registry = SourceRegistry()
    registry.register(_FakeAdapter("remoteok", ["a1", "a2"]))
    registry.register(_FakeAdapter("greenhouse", ["b1"]))

    orchestrator = ParsingOrchestrator(
        session=_OkSession(), sources=["remoteok", "greenhouse"], registry=registry
    )

    async with AsyncSession(engine, expire_on_commit=False) as session:
        outcome = await orchestrator.run_auto(
            session, user_id=user_id, keywords=["python"], max_pages=1
        )
        rows = list(
            (
                await session.scalars(
                    select(Vacancy).where(Vacancy.user_id == user_id)
                )
            ).all()
        )

    # SELECT без ORDER BY: физический порядок строк не гарантирован (flush
    # вставляет a1/a2 одним пакетом, порядок в выборке может инвертироваться).
    by_source: dict[str, list[str]] = {}
    for row in rows:
        by_source.setdefault(row.source, []).append(row.hh_vacancy_id)
    by_source = {source: sorted(ids) for source, ids in by_source.items()}

    assert by_source == {"remoteok": ["a1", "a2"], "greenhouse": ["b1"]}
    assert outcome.created == 3


# ============================================================
# Фильтр GET /vacancies?source=... (docs/03 §4, docs/04 §10.4)
# ============================================================


async def test_vacancies_filter_by_source(client):
    """Список фильтруется по площадке-источнику."""
    headers = await _auth(client, f"filter-{uuid.uuid4().hex[:8]}@test.dev")
    user_id = uuid.UUID(await user_id_for(client, headers))

    sources = ["hh", "remoteok", "greenhouse"]
    async with client.test_session_factory() as session:
        for index, source in enumerate(sources):
            session.add(
                Vacancy(
                    user_id=user_id,
                    hh_vacancy_id=f"src{index}",
                    url=f"https://example/{index}",
                    source=source,
                    status="raw",
                    title=f"vacancy {index}",
                )
            )
        await session.commit()

    listing = await client.get(f"{API}/vacancies", headers=headers)
    assert listing.status_code == 200, listing.text
    assert listing.json()["total"] == 3

    filtered = await client.get(
        f"{API}/vacancies", params={"source": "remoteok"}, headers=headers
    )
    assert filtered.status_code == 200, filtered.text
    data = filtered.json()
    assert data["total"] == 1
    assert data["items"][0]["source"] == "remoteok"


# ============================================================
# Целостность БД: CHECK source + составной уникальный индекс (docs/02 §3.3)
# ============================================================


async def test_db_source_check_constraint(engine):
    """Недопустимое значение source нарушает CHECK ck_vacancies_source."""
    user_id = uuid.uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO users (id, email, password_hash) VALUES (:id, :e, 'h')"),
            {"id": user_id, "e": f"{user_id}@example.com"},
        )

    with pytest.raises(IntegrityError):
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO vacancies (user_id, hh_vacancy_id, url, status, source) "
                    "VALUES (:u, '1', 'https://hh.ru/vacancy/1', 'raw', 'unknown')"
                ),
                {"u": user_id},
            )


async def test_db_composite_source_external_unique(engine):
    """Дубль (user_id, source, external_id) нарушает уникальный индекс."""
    user_id = uuid.uuid4()
    insert = (
        "INSERT INTO vacancies (user_id, hh_vacancy_id, url, status, source) "
        "VALUES (:u, 'ext-1', 'https://example/1', 'raw', 'remoteok')"
    )
    async with engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO users (id, email, password_hash) VALUES (:id, :e, 'h')"),
            {"id": user_id, "e": f"{user_id}@example.com"},
        )
        await conn.execute(text(insert), {"u": user_id})

    with pytest.raises(IntegrityError):
        async with engine.begin() as conn:
            await conn.execute(text(insert), {"u": user_id})


# ============================================================
# Прогрев сессии не долбит hh.ru без hh в источниках (docs/04 §3.3, §10)
# ============================================================


def test_orchestrator_without_hh_disables_hh_warmup():
    """Автопоиск без hh: сессия создаётся с warmup_url=None (прогрев отключён).

    Регрессия: раньше AntiBanSession.fetch() ВСЕГДА прогревался на https://hh.ru/
    (docs/04 §3.3) перед первым запросом — даже когда hh не входит в выбранные
    источники. Из-за этого парсинг RemoteOK/Remotive/… падал на капче hh.ru ещё
    до первого реального обращения к целевому источнику.
    """
    orchestrator = ParsingOrchestrator(sources=["remoteok", "greenhouse"])
    assert orchestrator.http.warmup_url is None  # прогрев на hh.ru отключён
    assert orchestrator.http.warmed_up is True  # сессия «готова» без прогрева


def test_orchestrator_with_hh_keeps_hh_warmup():
    """Если среди источников есть hh — прогрев на hh.ru сохраняется (§3.3)."""
    from app.modules.anti_ban.constants import WARMUP_URL

    orchestrator = ParsingOrchestrator(sources=["hh", "remoteok"])
    assert orchestrator.http.warmup_url == WARMUP_URL
    assert orchestrator.http.warmed_up is False  # прогреем перед первым запросом


def test_orchestrator_default_sources_keep_hh_warmup():
    """Дефолт (sources=None → ['hh']) сохраняет прежнее поведение с прогревом."""
    from app.modules.anti_ban.constants import WARMUP_URL

    orchestrator = ParsingOrchestrator()
    assert orchestrator.sources == ["hh"]
    assert orchestrator.http.warmup_url == WARMUP_URL
    assert orchestrator.http.warmed_up is False


