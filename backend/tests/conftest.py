"""Интеграционные тесты Career-Assistant-AI (pytest + httpx + PostgreSQL).

Запуск:   cd backend && python -m pytest
БД:       отдельная БД career_assistant_test (создаётся автоматически),
          схема — app.db.base.Base.metadata (таблицы docs/02_DATABASE.md).
Подключение: DATABASE_URL из app.core.config (localhost:5432), при необходимости
          переопределяется переменной CA_TEST_DATABASE_URL.

Очередь задач в тестах — ARQ-job'ы без Redis: реальные ``run_parsing_task`` /
``run_llm_task`` вызываются напрямую с контекстом, а постановка задачи в
очередь проверяется через пул-заглушку RecordingPool. Это покрывает и
маршрутизацию в очередь, и исполнение, и запись состояния в БД.
"""

from __future__ import annotations

import asyncio
import os
import uuid

import asyncpg
import pytest
import pytest_asyncio
from arq.worker import Retry
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import settings
from app.db.base import Base
from app.db.session import get_db
from app.main import app
from app.modules.queue_manager.queues import (
    LLM_QUEUE,
    RecordingPool,
    enqueue_task,
)
from app.modules.queue_manager.slots import InProcessQueueSlots
from app.modules.queue_manager.worker import run_llm_task, run_parsing_task

# --- URL'ы: dev-BD из настроек → test-BD рядом (только имя меняется) ---
_BASE_URL = settings.database_url.rsplit("/", 1)[0]
TEST_DB_URL = os.environ.get(
    "CA_TEST_DATABASE_URL", f"{_BASE_URL}/career_assistant_test"
)
ADMIN_DB_URL = f"{_BASE_URL}/postgres"
TEST_DB_NAME = TEST_DB_URL.rsplit("/", 1)[-1].split("?")[0]


def _ensure_test_database() -> None:
    """Создаёт тестовую БД, если её ещё нет (синхронно, вне event-loop pytest)."""

    async def run() -> None:
        conn = await asyncpg.connect(ADMIN_DB_URL.replace("+asyncpg", ""))
        try:
            exists = await conn.fetchval(
                "SELECT 1 FROM pg_database WHERE datname = $1", TEST_DB_NAME
            )
            if not exists:
                await conn.execute(f'CREATE DATABASE "{TEST_DB_NAME}"')
        finally:
            await conn.close()

    asyncio.run(run())


@pytest.fixture(scope="session")
def engine():
    """Сессионный engine тестовой БД (NullPool — нет переиспользования
    соединений между event-loop'ами pytest)."""
    _ensure_test_database()
    test_engine = create_async_engine(TEST_DB_URL, poolclass=NullPool)

    async def _init_schema() -> None:
        async with test_engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(_init_schema())
    yield test_engine
    asyncio.run(test_engine.dispose())


@pytest_asyncio.fixture
async def client(engine, queue_pool):
    """ASGI-клиент поверх реального приложения с подменой get_db на тестовую БД.

    Зависит от ``queue_pool``, поэтому задачи, созданные через API, попадают в
    пул-заглушку и тесты не обращаются к реальному Redis.
    """
    test_session_factory = async_sessionmaker(
        bind=engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
    )

    async def override_get_db():
        async with test_session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest_asyncio.fixture(autouse=True)
async def _clean_tables(engine):
    """Каждый тест стартует с пустой БД."""
    yield
    async with engine.begin() as conn:
        await conn.execute(text("TRUNCATE TABLE users RESTART IDENTITY CASCADE"))


# --- очередь задач: ARQ без Redis ----------------------------------------


@pytest.fixture
def queue_pool(monkeypatch):
    """Пул-заглушка Redis: задачи пишутся в список вместо реального Redis.

    Подменяет get_pool() во всём приложении, поэтому и enqueue_task(), и
    publish_event() (шина Realtime Module) работают без внешних сервисов.
    """
    pool = RecordingPool()

    async def _fake_get_pool():
        return pool

    monkeypatch.setattr(
        "app.modules.queue_manager.queues.get_pool", _fake_get_pool, raising=True
    )
    monkeypatch.setattr(
        "app.modules.realtime.bus.get_pool", _fake_get_pool, raising=False
    )
    return pool


class QueueRunner:
    """Прогон ARQ-job'ов очереди без Redis (тестовая замена воркера).

    В реальном приложении job достаётся из отсортированного множества Redis,
    здесь же он берётся из RecordingPool — но исполняется тот же самый
    ``run_parsing_task`` / ``run_llm_task`` с тем же контекстом.
    """

    def __init__(self, pool: RecordingPool, session_factory) -> None:
        self.pool = pool
        self.session_factory = session_factory
        # Слоты общие на весь прогон — ограничения параллелизма проверяются
        # теми же средствами, что и в проде (Redis-семафор).
        self.slots = InProcessQueueSlots()

    def _ctx(self) -> dict:
        return {"session_factory": self.session_factory, "slots": self.slots}

    async def _run_job(self, job: dict):
        task_id = job["args"][0]
        if job["queue_name"] == LLM_QUEUE:
            return await run_llm_task(self._ctx(), task_id)
        return await run_parsing_task(self._ctx(), task_id)

    async def drain(self, *, max_passes: int = 5) -> None:
        """Выполнить все job'ы из очереди, включая отложенные по слотам.

        Отложенный job (Retry из-за занятого слота) возвращается в очередь и
        обрабатывается на следующем проходе — как это делает ARQ-воркер.
        """
        for _ in range(max_passes):
            if not self.pool.jobs:
                return
            batch, self.pool.jobs = list(self.pool.jobs), []
            for job in batch:
                try:
                    await self._run_job(job)
                except Retry:
                    self.pool.jobs.append(job)  # вернули в очередь
        if self.pool.jobs:
            raise AssertionError("Очередь не опустела: остались отложенные задачи")

    async def run_pending(self, engine, task_ids: list) -> None:
        """Поставить указанные задачи в очередь и выполнить их."""
        from sqlalchemy import select

        from app.db.models import Task

        factory = async_sessionmaker(
            bind=engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
        )
        async with factory() as session:
            for task_id in task_ids:
                task = await session.get(Task, uuid.UUID(str(task_id)))
                assert task is not None, f"Задача {task_id} не найдена"
                await enqueue_task(task.id, task.task_type, pool=self.pool)
        await self.drain()

    async def run_all_pending(self, engine) -> None:
        """Выполнить все pending-задачи пользователей (основной сценарий тестов)."""
        from sqlalchemy import select

        from app.db.models import Task

        factory = async_sessionmaker(
            bind=engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
        )
        async with factory() as session:
            tasks = list(
                (
                    await session.scalars(
                        select(Task).where(Task.status == "pending")
                    )
                ).all()
            )
            for task in tasks:
                await enqueue_task(task.id, task.task_type, pool=self.pool)
        await self.drain()


@pytest.fixture
def queue_runner(engine, queue_pool) -> QueueRunner:
    """Прогонщик очереди, привязанный к тестовой БД."""
    factory = async_sessionmaker(
        bind=engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
    )
    return QueueRunner(queue_pool, factory)


@pytest_asyncio.fixture
async def redis_client():
    """Настоящий Redis для проверок семафоров (SET NX EX).

    Если Redis недоступен, тесты этих проверок пропускаются: они не проверяют
    логику приложения, а работу с реальным сервером Redis.
    """
    import redis.asyncio as aioredis

    from app.core.config import settings as app_settings

    client = aioredis.from_url(app_settings.redis_url)
    try:
        await client.ping()
    except Exception as exc:  # noqa: BLE001 — внешний сервис
        await client.aclose()
        pytest.skip(f"Redis недоступен: {exc}")
    try:
        yield client
    finally:
        await client.aclose()
