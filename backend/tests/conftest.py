"""Интеграционные тесты Career-Assistant-AI (pytest + httpx + PostgreSQL).

Запуск:   cd backend && python -m pytest
БД:       отдельная БД career_assistant_test (создаётся автоматически),
          схема — app.db.base.Base.metadata (таблицы docs/02_DATABASE.md).
Подключение: DATABASE_URL из app.core.config (localhost:5432), при необходимости
          переопределяется переменной CA_TEST_DATABASE_URL.
"""

from __future__ import annotations

import asyncio
import os

import asyncpg
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import settings
from app.db.base import Base
from app.db.session import get_db
from app.main import app

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
async def client(engine):
    """ASGI-клиент поверх реального приложения с подменой get_db на тестовую БД."""
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
