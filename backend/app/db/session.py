"""Асинхронное подключение к PostgreSQL: engine, сессии, FastAPI dependency.

SQLAlchemy 2.0 async + asyncpg (DATABASE_URL из backend/.env, см. app.core.config).
"""

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings

engine = create_async_engine(
    settings.database_url,
    echo=settings.debug,
    pool_pre_ping=True,
    pool_size=10,
    max_overflow=20,
)

AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


async def get_db() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency: одна сессия на запрос, закрытие в конце запроса."""
    async with AsyncSessionLocal() as session:
        yield session
