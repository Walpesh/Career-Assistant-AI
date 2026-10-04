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

Письма с OTP-кодом в тестах не отправляются: автофикстура ``_capture_otp``
подменяет ``app.core.mail.send_verification_email`` на запись кода в
``OTP_OUTBOX`` (email → код). Тесты получают код оттуда и подтверждают email
через настоящий POST /auth/verify-email.
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

#: «Почтовый ящик» тестов: email → последний выданный 6-значный OTP-код.
#: Заполняется автофикстурой ``_capture_otp`` вместо реальной отправки SMTP.
OTP_OUTBOX: dict[str, str] = {}

#: Префикс API (docs/03_API_CONTRACTS.md §1).
API = settings.api_prefix

#: Стандартный пароль тестовых пользователей.
PASSWORD = "strongpassword"

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
        # Фабрика нужна помощникам, которые правят БД мимо API (например,
        # отзыв сессии, созданной подтверждением email).
        test_client.test_session_factory = test_session_factory
        yield test_client
    app.dependency_overrides.clear()


@pytest_asyncio.fixture(autouse=True)
async def _clean_tables(engine):
    """Каждый тест стартует с пустой БД."""
    yield
    async with engine.begin() as conn:
        await conn.execute(text("TRUNCATE TABLE users RESTART IDENTITY CASCADE"))
        # email_otps не имеет FK на users — чистим явно, иначе код прошлого
        # теста остался бы в БД и следующий тест получил бы чужой OTP.
        await conn.execute(text("TRUNCATE TABLE email_otps"))


@pytest.fixture(autouse=True)
def _capture_otp(monkeypatch):
    """Перехватывать отправку писем: код кладётся в OTP_OUTBOX.

    SMTP в тестах не используется (ни реального сервера, ни его отсутствия):
    подменяется сама отправка, поэтому тест работает и при настроенном
    ``SMTP_HOST``, и без него. ``BackgroundTasks`` выполняются Starlette
    до возврата ответа тестовому клиенту, поэтому код доступен сразу после
    POST /auth/register.
    """
    from app.core import mail as mail_module

    async def _fake_send(email: str, code: str, **_: object) -> bool:
        OTP_OUTBOX[email.strip().lower()] = code
        return True

    monkeypatch.setattr(mail_module, "send_verification_email", _fake_send)
    OTP_OUTBOX.clear()
    yield OTP_OUTBOX
    OTP_OUTBOX.clear()


# --- Общие помощники регистрации с подтверждением email ---------------------
# Тесты, которым нужен «обычный» авторизованный пользователь, не должны
# повторять связку register → OTP → login: после введения верификации
# login не выдаёт токены неподтверждённому аккаунту (docs/03 §2).


async def confirm_email(client, email: str, code: str | None = None) -> dict:
    """Подтвердить email выданным OTP-кодом → тело ответа verify-email."""
    target = email.strip().lower()
    actual = code or OTP_OUTBOX.get(target)
    assert actual, f"OTP-код для {target} не был «отправлен» (OTP_OUTBOX пуст)"
    response = await client.post(
        f"{API}/auth/verify-email", json={"email": email, "code": actual}
    )
    assert response.status_code == 200, response.text
    return response.json()


async def register_verified(client, email: str, password: str = PASSWORD) -> dict:
    """Полный флоу нового пользователя: register → OTP → пара JWT.

    Заменяет старую связку register + login во всех тестах, которым не нужно
    отдельно проверять поведение неподтверждённого аккаунта.
    """
    response = await client.post(
        f"{API}/auth/register", json={"email": email, "password": password}
    )
    assert response.status_code == 201, response.text
    return await confirm_email(client, email)


async def auth_headers_for(
    client, email: str | None = None, password: str = PASSWORD
) -> dict[str, str]:
    """Зарегистрировать (при необходимости) и вернуть заголовок Bearer JWT."""
    target = email or f"t{uuid.uuid4().hex[:10]}@test.dev"
    tokens = await register_verified(client, target, password)
    return {"Authorization": f"Bearer {tokens['access_token']}"}


async def user_id_for(client, headers: dict[str, str]) -> str:
    """id текущего пользователя по Bearer-заголовку (GET /auth/me)."""
    response = await client.get(f"{API}/auth/me", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["id"]


async def register_verified_without_session(
    client, email: str, password: str = PASSWORD
) -> str:
    """register → OTP → подтверждение, затем отзыв сессии. Возвращает user_id.

    Подтверждение email само по себе выдаёт refresh-токен (docs/03 §2), поэтому
    после ``register_verified`` у пользователя есть одна «лишняя» сессия.
    Тестам, которые считают сессии (ротация, revoke-all, реестр
    ``refresh_tokens``), она мешает — этот помощник возвращает БД в состояние
    «ноль активных сессий», как было до введения верификации.

    Сессия отзывается напрямую в БД через фабрику тестового клиента, поэтому
    отзыв всегда попадает в тестовую, а не в дефолтную БД.
    """
    from app.modules.auth.security import TOKEN_TYPE_ACCESS, decode_token
    from app.modules.auth.tokens import revoke_all_user_tokens

    tokens = await register_verified(client, email, password)
    user_id = decode_token(tokens["access_token"], expected_type=TOKEN_TYPE_ACCESS)["sub"]

    async with client.test_session_factory() as session:
        await revoke_all_user_tokens(session, uuid.UUID(user_id))
        await session.commit()
    return user_id


@pytest.fixture(autouse=True)
def _disable_rate_limit(monkeypatch):
    """По умолчанию лимиты выключены, чтобы не пересекаться между тестами.

    Реальный лимитер тестируется отдельно (tests/test_security.py), где он
    включается явно и использует реальный Redis.
    """
    monkeypatch.setattr(settings, "rate_limit_enabled", False, raising=False)


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
