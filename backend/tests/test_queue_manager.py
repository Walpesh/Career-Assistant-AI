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


