"""Тесты очереди задач на Redis + ARQ (docs/01 §3, docs/04 §6).

Покрывают то, что изменилось при переходе с DB short-polling на событийную
очередь Redis:

    - постановка задачи в очередь происходит сразу при создании (enqueue_job);
    - парсинг и LLM попадают в разные очереди (docs/04 §6);
    - job_id дедуплицирует постановку одной и той же задачи;
    - приоритет «ручное → групповое → авто» задаётся score'ом в очереди;
    - слоты ограничивают параллелизм и освобождаются после выполнения;
    - конфигурация короткого опроса БД удалена.

Redis в тестах не поднимается: пул-заглушка RecordingPool повторяет
поведение настоящего enqueue_job (дедупликация по _job_id, сортировка job'ов
по score, приём событий Realtime Module через publish).
"""

from __future__ import annotations

import uuid

import pytest

from app.core.config import settings
from app.modules.queue_manager.queues import (
    LLM_JOB,
    LLM_QUEUE,
    PARSING_JOB,
    PARSING_QUEUE,
    QueueUnavailable,
    abort_job,
    enqueue_task,
    queue_for_task_type,
    recover_pending_tasks,
)
from app.modules.queue_manager.slots import (
    InProcessQueueSlots,
    LLM_SLOT_GROUP,
    QueueBusy,
    RedisQueueSlots,
    parsing_slot_group,
)
from app.modules.queue_manager.worker import (
    LLMQueueSettings,
    MAX_CONCURRENT_LLM_WORKERS,
    MAX_CONCURRENT_PARSERS_PER_USER,
    PARSING_TASK_TYPES,
    ParsingQueueSettings,
    TASK_PRIORITY,
)


# --- маршрутизация очередей (docs/04 §6) ----------------------------------


@pytest.mark.parametrize(
    ("task_type", "expected_queue", "expected_job"),
    [
        ("parse_manual", PARSING_QUEUE, PARSING_JOB),
        ("parse_group", PARSING_QUEUE, PARSING_JOB),
        ("parse_auto", PARSING_QUEUE, PARSING_JOB),
        ("analyze", LLM_QUEUE, LLM_JOB),
        ("generate_letter", LLM_QUEUE, LLM_JOB),
        ("auto_full", LLM_QUEUE, LLM_JOB),
        ("convert_resume", LLM_QUEUE, LLM_JOB),
    ],
)
async def test_task_type_routes_to_expected_queue_and_job(
    queue_pool, task_type, expected_queue, expected_job
):
    """Каждый тип задачи уходит в свою очередь и на свою job-функцию (docs/04 §6)."""
    assert queue_for_task_type(task_type) == expected_queue

    task_id = uuid.uuid4()
    await enqueue_task(task_id, task_type, pool=queue_pool)

    assert len(queue_pool.jobs) == 1
    job = queue_pool.jobs[0]
    assert job["queue_name"] == expected_queue
    assert job["function"] == expected_job
    assert job["args"] == (str(task_id),)


async def test_parsing_and_llm_queues_are_distinct(queue_pool):
    """docs/04 §6: парсинг и LLM не смешиваются в одной очереди."""
    assert PARSING_QUEUE != LLM_QUEUE

    for task_type in PARSING_TASK_TYPES:
        await enqueue_task(uuid.uuid4(), task_type, pool=queue_pool)
    for task_type in ("analyze", "generate_letter", "auto_full"):
        await enqueue_task(uuid.uuid4(), task_type, pool=queue_pool)

    assert queue_pool.queue_len(PARSING_QUEUE) == len(PARSING_TASK_TYPES)
    assert queue_pool.queue_len(LLM_QUEUE) == 3


async def test_enqueue_deduplicates_same_task(queue_pool):
    """Повторная постановка одной задачи не создаёт вторую работу."""
    task_id = uuid.uuid4()

    first = await enqueue_task(task_id, "parse_manual", pool=queue_pool)
    second = await enqueue_task(task_id, "parse_manual", pool=queue_pool)

    assert first is not None
    assert second is None  # enqueue_job вернул None — job_id уже существует
    assert len(queue_pool.jobs) == 1


async def test_enqueue_raises_when_redis_unavailable(monkeypatch):
    """Без Redis задача не «теряется молча», а поднимает QueueUnavailable."""
    from app.modules.queue_manager import queues as queues_module

    async def _no_pool():
        return None

    monkeypatch.setattr(queues_module, "get_pool", _no_pool)
    with pytest.raises(QueueUnavailable):
        await enqueue_task(uuid.uuid4(), "parse_manual")


# --- приоритет в очереди (docs/04 §6) --------------------------------------


async def test_priority_is_encoded_in_queue_score(queue_pool):
    """Ручное → групповое → авто: порядок задаётся score'ом в очереди Redis.

    ARQ выбирает job'ы по возрастанию score, поэтому меньший score = выше
    приоритет. Все три задачи берутся сразу (score в прошлом), но manual
    оказывается первым в порядке выдачи.
    """
    manual = uuid.uuid4()
    group = uuid.uuid4()
    auto = uuid.uuid4()

    await enqueue_task(auto, "parse_auto", pool=queue_pool)
    await enqueue_task(manual, "parse_manual", pool=queue_pool)
    await enqueue_task(group, "parse_group", pool=queue_pool)

    order = [job["args"][0] for job in queue_pool.jobs]
    assert order == [str(manual), str(group), str(auto)]

    scores = [job["score"] for job in queue_pool.jobs]
    assert scores == sorted(scores)  # очередь отсортирована по score


async def test_llm_jobs_have_no_priority_offset(queue_pool):
    """LLM-задачи не смещаются по score — они в отдельной очереди (docs/04 §6)."""
    for _ in range(2):
        await enqueue_task(uuid.uuid4(), "analyze", pool=queue_pool)

    scores = [job["score"] for job in queue_pool.jobs]
    # Смещение применяется только к задачам парсинга, у analyze оно нулевое.
    assert max(scores) - min(scores) < 1000


# --- слоты параллелизма (docs/04 §1, §6) ----------------------------------


async def test_parsing_slots_limit_to_two_per_user(redis_client):
    """docs/04 §1: не более 2 одновременных парсинг-воркеров на пользователя.

    Проверяется на настоящем Redis: захват слота — это SET NX EX, поэтому
    ограничение действительно работает между процессами.
    """
    slots = RedisQueueSlots(redis_client)
    group = parsing_slot_group(uuid.uuid4())

    first = await slots.acquire(group, MAX_CONCURRENT_PARSERS_PER_USER)
    second = await slots.acquire(group, MAX_CONCURRENT_PARSERS_PER_USER)
    third = await slots.acquire(group, MAX_CONCURRENT_PARSERS_PER_USER)

    assert first is not None and second is not None
    assert third is None  # оба слота заняты

    # Другой пользователь — независимая группа слотов.
    other_group = parsing_slot_group(uuid.uuid4())
    other = await slots.acquire(other_group, MAX_CONCURRENT_PARSERS_PER_USER)
    assert other is not None

    # Освобождаем слоты, чтобы не оставить ключи в Redis.
    for lease in (first, second):
        index, token = lease
        await slots.release(group, index, token)
    index, token = other
    await slots.release(other_group, index, token)


async def test_slot_is_released_after_use(redis_client):
    """Освободившийся слот тут же занимается следующей задачей."""
    slots = RedisQueueSlots(redis_client)
    group = parsing_slot_group(uuid.uuid4())

    first = await slots.acquire(group, MAX_CONCURRENT_PARSERS_PER_USER)
    second = await slots.acquire(group, MAX_CONCURRENT_PARSERS_PER_USER)
    assert first is not None and second is not None

    # Оба слота заняты — третья задача ждёт очереди (docs/04 §5).
    assert await slots.acquire(group, MAX_CONCURRENT_PARSERS_PER_USER) is None

    index, token = first
    await slots.release(group, index, token)

    # Освободившийся слот занимается следующей задачей.
    lease = await slots.acquire(group, MAX_CONCURRENT_PARSERS_PER_USER)
    assert lease is not None

    index, token = second
    await slots.release(group, index, token)
    index, token = lease
    await slots.release(group, index, token)


async def test_llm_slot_is_global_and_single():
    """docs/04 §6: LLM-очередь — один воркер на всё приложение."""
    assert MAX_CONCURRENT_LLM_WORKERS == 1
    assert LLMQueueSettings.as_worker_kwargs()["max_jobs"] == 1

    slots = InProcessQueueSlots()
    first = await slots.acquire(LLM_SLOT_GROUP, MAX_CONCURRENT_LLM_WORKERS)
    second = await slots.acquire(LLM_SLOT_GROUP, MAX_CONCURRENT_LLM_WORKERS)

    assert first is not None
    assert second is None  # глобальный слот занят независимо от пользователя


async def test_slot_released_only_by_owner():
    """Слот освобождает только его владелец (по токену) — защита от подмены."""
    slots = InProcessQueueSlots()
    group = parsing_slot_group(uuid.uuid4())

    index, token = await slots.acquire(group, 1)
    await slots.release(group, index, "чужой-токен")  # не наш слот
    assert await slots.acquire(group, 1) is None  # слот всё ещё занят

    await slots.release(group, index, token)
    assert await slots.acquire(group, 1) is not None


def test_queue_busy_error_is_available():
    """QueueBusy — сигнал «слот занят, задача ждёт очереди»."""
    assert issubclass(QueueBusy, RuntimeError)


# --- настройки ARQ-воркеров ------------------------------------------------


def test_worker_settings_split_parsing_and_llm():
    """docs/04 §6: раздельные настройки воркеров для двух очередей."""
    parsing = ParsingQueueSettings.as_worker_kwargs()
    llm = LLMQueueSettings.as_worker_kwargs()

    assert parsing["queue_name"] == PARSING_QUEUE
    assert llm["queue_name"] == LLM_QUEUE
    assert parsing["functions"] == [PARSING_JOB]
    assert llm["functions"] == [LLM_JOB]
    # Парсинг масштабируется горизонтально, LLM — строго один поток (docs/01 §6).
    assert parsing["max_jobs"] == settings.arq_parsing_max_jobs
    assert llm["max_jobs"] == 1


def test_worker_settings_graceful_shutdown_hooks():
    """WorkerSettings: SIGTERM-обработка + startup/shutdown хуки (production)."""
    from app.modules.queue_manager import worker as worker_module

    for cls in (ParsingQueueSettings, LLMQueueSettings):
        kwargs = cls.as_worker_kwargs()
        # ARQ сам обрабатывает SIGTERM/SIGINT: активные job'ы завершаются.
        assert kwargs["handle_signals"] is True
        assert cls.handle_signals is True
        # Startup: восстановление задач под Redis-блокировкой SET NX EX.
        assert kwargs["on_startup"] is worker_module._on_worker_startup
        assert kwargs["on_shutdown"] is worker_module._on_worker_shutdown
        assert callable(worker_module.install_signal_handlers)


async def test_recover_lock_single_winner(queue_pool):
    """SET NX EX: только один процесс выполняет recover_pending_tasks."""
    from app.modules.queue_manager.queues import RECOVER_LOCK_KEY, acquire_recover_lock

    assert await acquire_recover_lock(queue_pool) is True
    assert await acquire_recover_lock(queue_pool) is False
    # Другой пул (другая реплика) тоже видит занятую блокировку глобально?
    # RecordingPool локален — проверяем контракт на том же пуле.
    assert queue_pool._kv[RECOVER_LOCK_KEY] == "1"


async def test_recover_skipped_when_lock_taken(engine, queue_pool):
    """Процесс без блокировки возвращает 0 и не трогает БД."""
    from app.modules.queue_manager import queues as queues_module

    async def _locked(_redis: object, ttl_seconds: int | None = None) -> bool:
        _ = (ttl_seconds,)
        return False

    monkeypatch_lock = _locked
    orig = queues_module.acquire_recover_lock
    queues_module.acquire_recover_lock = monkeypatch_lock  # type: ignore[assignment]
    try:
        factory, _ = await _make_task_row(
            engine, "parse_manual", "pending", {"vacancy_url": "https://hh.ru/vacancy/1"}
        )
        assert await recover_pending_tasks(factory, pool=queue_pool) == 0
        assert queue_pool.jobs == []
    finally:
        queues_module.acquire_recover_lock = orig


def test_worker_job_paths_are_importable():
    """Пути job-функций резолвятся в реальные корутины (иначе ARQ не найдёт их)."""
    import importlib

    for job_path in (PARSING_JOB, LLM_JOB):
        module_path, func_name = job_path.rsplit(".", 1)
        module = importlib.import_module(module_path)
        coroutine = getattr(module, func_name)
        assert callable(coroutine)
        assert "ctx" in coroutine.__code__.co_varnames


def test_no_db_polling_configuration_remains():
    """Конфигурация короткого опроса БД удалена полностью.

    Раньше был `queue_poll_interval_seconds` и цикл `SELECT ... WHERE
    status='pending'` каждые 2 секунды. Теперь работа приходит из Redis,
    поэтому такой настройки в Settings быть не должно.
    """
    assert not hasattr(settings, "queue_poll_interval_seconds")
    assert hasattr(settings, "arq_poll_delay_seconds")
    assert hasattr(settings, "arq_queue_parsing")
    assert hasattr(settings, "arq_queue_llm")


def test_priority_order_constants_preserved():
    """docs/04 §6: приоритеты ручное → групповое → авто сохранены."""
    assert TASK_PRIORITY["parse_manual"] < TASK_PRIORITY["parse_group"]
    assert TASK_PRIORITY["parse_group"] < TASK_PRIORITY["parse_auto"]


# --- восстановление после сбоя --------------------------------------------


async def _make_task_row(engine, task_type: str, status: str, payload: dict):
    """Создать пользователя с задачей в указанном статусе."""
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from app.db.models import Task, User

    factory = async_sessionmaker(
        bind=engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
    )
    async with factory() as session:
        user = User(email=f"q{uuid.uuid4().hex[:10]}@test.dev", password_hash="x")
        session.add(user)
        await session.flush()
        task = Task(
            user_id=user.id, task_type=task_type, status=status, payload=payload
        )
        session.add(task)
        await session.commit()
        return factory, task.id


async def test_recover_pending_tasks_reenqueues_pending_tasks(engine, queue_pool):
    """Задачи, застрявшие в pending, возвращаются в очередь при старте."""
    factory, task_id = await _make_task_row(
        engine, "parse_manual", "pending", {"vacancy_url": "https://hh.ru/vacancy/1"}
    )

    restored = await recover_pending_tasks(factory, pool=queue_pool)

    assert restored == 1
    assert [job["args"][0] for job in queue_pool.jobs] == [str(task_id)]


async def test_recover_resets_interrupted_processing_tasks(engine, queue_pool):
    """Задача, начатая до сбоя, возвращается в pending и ставится в очередь заново."""
    from app.db.models import Task

    factory, task_id = await _make_task_row(
        engine, "parse_auto", "processing", {"keywords": ["x"]}
    )

    restored = await recover_pending_tasks(factory, pool=queue_pool)
    assert restored == 1

    async with factory() as session:
        recovered = await session.get(Task, task_id)
    # Обработка начнётся заново: статус pending, started_at проставится заново.
    assert recovered.status == "pending"
    assert [job["args"][0] for job in queue_pool.jobs] == [str(task_id)]


async def test_recover_skips_finished_tasks(engine, queue_pool):
    """Завершённые и упавшие задачи в очередь не возвращаются."""
    from app.db.models import Task

    factory, done_id = await _make_task_row(
        engine, "parse_manual", "completed", {"vacancy_url": "https://hh.ru/vacancy/2"}
    )

    restored = await recover_pending_tasks(factory, pool=queue_pool)

    assert restored == 0
    assert queue_pool.jobs == []
    async with factory() as session:
        recovered = await session.get(Task, done_id)
    assert recovered.status == "completed"


async def test_recover_without_redis_is_noop(engine, monkeypatch):
    """Если Redis недоступен, восстановление не падает и не теряет задачи."""
    from app.modules.queue_manager import queues as queues_module

    factory, task_id = await _make_task_row(
        engine, "parse_manual", "pending", {"vacancy_url": "https://hh.ru/vacancy/3"}
    )

    async def _no_pool():
        return None

    monkeypatch.setattr(queues_module, "get_pool", _no_pool)

    assert await recover_pending_tasks(factory) == 0


async def test_recover_reenqueues_when_job_id_deduplicated(engine, queue_pool):
    """Потеря задачи при восстановлении: дедупликация _job_id → abort + суффикс.

    После сбоя ключ зависшего job'а ещё жив в Redis: наивный enqueue_task
    вернёт None и задача «повиснет» (в БД pending, в очереди — нет). recover
    обязан снять старый job и пере-поставить с уникальным attempt-суффиксом.
    """
    factory, task_id = await _make_task_row(
        engine, "parse_manual", "pending", {"vacancy_url": "https://hh.ru/vacancy/9"}
    )

    # «Старый» job с тем же _job_id уже есть в очереди (ключ пережил сбой).
    stale = await queue_pool.enqueue_job(
        PARSING_JOB,
        str(task_id),
        _job_id=f"parse_manual:{task_id}",
        _queue_name=PARSING_QUEUE,
    )
    assert stale is not None

    restored = await recover_pending_tasks(factory, pool=queue_pool)

    assert restored == 1
    assert len(queue_pool.jobs) == 1  # старый снят, новый поставлен
    job_id = queue_pool.jobs[0]["job_id"]
    assert job_id.startswith(f"parse_manual:{task_id}:recover-")
    assert queue_pool.jobs[0]["args"] == (str(task_id),)


async def test_abort_job_removes_job_from_queue(queue_pool):
    """abort_job снимает job из очереди — основа отмены задачи (docs/04 §6)."""
    task_id = uuid.uuid4()
    await enqueue_task(task_id, "parse_auto", pool=queue_pool)
    assert len(queue_pool.jobs) == 1

    assert await abort_job(task_id, "parse_auto", pool=queue_pool) is True
    assert queue_pool.jobs == []
    # Повторный abort — идемпотентен.
    assert await abort_job(task_id, "parse_auto", pool=queue_pool) is False


# --- отмена/возобновление через API (docs/03 §7) ----------------------------

API = "/api/v1"
_PASSWORD = "strongpassword"


async def _register_and_login(client) -> tuple[str, dict]:
    """Регистрация + подтверждение email → (user_id, auth-заголовки).

    Токены выдаёт POST /auth/verify-email: до подтверждения email вход
    запрещён (docs/03 §2).
    """
    from conftest import register_verified, user_id_for

    email = f"q{uuid.uuid4().hex[:10]}@test.dev"
    tokens = await register_verified(client, email, _PASSWORD)
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}
    return await user_id_for(client, headers), headers


async def _create_task_row(
    engine,
    user_id,
    *,
    task_type: str = "parse_auto",
    status: str = "pending",
    related_vacancy_id=None,
):
    """Задача конкретного пользователя (для endpoint'ов /tasks/*)."""
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.db.models import Task

    async with AsyncSession(engine, expire_on_commit=False) as session:
        task = Task(
            user_id=uuid.UUID(str(user_id)),
            task_type=task_type,
            status=status,
            payload={"keywords": ["x"]},
            related_vacancy_id=related_vacancy_id,
        )
        session.add(task)
        await session.commit()
        await session.refresh(task)
        return task.id


async def test_cancel_aborts_job_and_publishes_events(client, engine, queue_pool):
    """Отмена: job снимается из Redis, статус failed + события в UI (docs/03 §8)."""
    import json as jsonlib

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.db.models import Task

    user_id, headers = await _register_and_login(client)
    task_id = await _create_task_row(engine, user_id)

    # Задача уже стоит в очереди — отмена обязана её оттуда убрать.
    await enqueue_task(task_id, "parse_auto", pool=queue_pool)
    assert len(queue_pool.jobs) == 1

    response = await client.post(f"{API}/tasks/{task_id}/cancel", headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["cancelled"] is True and body["status"] == "failed"

    # 1) Job снят из очереди Redis (иначе воркер подхватит отменённую задачу).
    assert queue_pool.jobs == []

    # 2) События task.cancelled + task.failed ушли в Realtime Module.
    events = {jsonlib.loads(m)["event"] for m in queue_pool.published}
    assert {"task.cancelled", "task.failed"} <= events

    # 3) Статус в БД: failed + timezone-aware finished_at (docs/02 §3.6).
    async with AsyncSession(engine, expire_on_commit=False) as session:
        task = await session.get(Task, task_id)
    assert task.status == "failed"
    assert task.error_message == "Отменено пользователем"
    assert task.finished_at is not None and task.finished_at.tzinfo is not None


async def test_cancel_blocked_when_vacancy_applied(client, engine, queue_pool):
    """docs/02 §5: applied — терминальный статус, отмена задачи блокируется (409)."""
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.db.models import Task, Vacancy

    user_id, headers = await _register_and_login(client)
    async with AsyncSession(engine, expire_on_commit=False) as session:
        vacancy = Vacancy(
            user_id=uuid.UUID(str(user_id)),
            hh_vacancy_id=uuid.uuid4().hex[:8],
            url="https://hh.ru/vacancy/1",
            title="Python Dev",
            status="applied",
            source="manual",
        )
        session.add(vacancy)
        await session.commit()
        await session.refresh(vacancy)
        vacancy_id = vacancy.id

    task_id = await _create_task_row(
        engine, user_id, task_type="analyze", related_vacancy_id=vacancy_id
    )

    response = await client.post(f"{API}/tasks/{task_id}/cancel", headers=headers)
    assert response.status_code == 409, response.text
    assert response.json()["error_code"] == "VACANCY_APPLIED"

    async with AsyncSession(engine, expire_on_commit=False) as session:
        task = await session.get(Task, task_id)
    assert task.status == "pending"  # отмены не было


async def test_resume_returns_waiting_captcha_task_to_queue(client, engine, queue_pool):
    """POST /tasks/{id}/resume: waiting_captcha → pending + job в очереди."""
    import json as jsonlib

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.db.models import Task

    user_id, headers = await _register_and_login(client)
    task_id = await _create_task_row(engine, user_id, status="waiting_captcha")

    # Resume принимает только waiting_captcha.
    other_id = await _create_task_row(engine, user_id, status="pending")
    blocked = await client.post(f"{API}/tasks/{other_id}/resume", headers=headers)
    assert blocked.status_code == 409
    assert blocked.json()["error_code"] == "TASK_NOT_WAITING_CAPTCHA"

    response = await client.post(f"{API}/tasks/{task_id}/resume", headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "pending"

    # Задача снова в очереди Redis + событие task.resumed для UI.
    assert [job["args"][0] for job in queue_pool.jobs] == [str(task_id)]
    events = {jsonlib.loads(m)["event"] for m in queue_pool.published}
    assert "task.resumed" in events

    async with AsyncSession(engine, expire_on_commit=False) as session:
        task = await session.get(Task, task_id)
    assert task.status == "pending"
    assert task.error_message is None


