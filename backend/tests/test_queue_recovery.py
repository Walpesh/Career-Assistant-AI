"""Восстановление очереди задач и отмена (docs/01 §3, docs/03 §7, docs/04 §6).

Архитектура (docs/01 §3): работа живёт **в Redis**, а таблица ``tasks`` —
источник состояния. Отсюда два обязательных свойства, которые и проверяются:

1. **Отмена снимает job из Redis.** Иначе воркер подхватит уже отменённую
   задачу и выполнит её: пользователь отменил анализ, а письмо всё равно
   сгенерировалось (docs/03 §7, docs/04 §6).
2. **Восстановление после падения возвращает работу в очередь без потерь.**
   Задачи, застрявшие в ``pending``/``processing`` из-за аварийной остановки
   процесса, возвращаются в очередь при старте
   (``recover_pending_tasks``, docs/01 §3, docs/04 §6):
     - ``processing`` → ``pending`` (задача не была финализирована);
     - ``finished_at`` не выставляется (работа не завершена);
     - ``payload``/``progress`` сохраняются — данные не теряются;
     - терминальные задачи (``completed``/``failed``) не перезапускаются;
     - восстановление защищено Redis-блокировкой SET NX (docs/01 §3), чтобы
       несколько реплик не поставили одну задачу дважды.

Redis в тестах заменён пулом-заглушкой ``RecordingPool``: он повторяет
поведение настоящего ``enqueue_job`` (дедупликация по ``_job_id``, сортировка
job'ов по score) и ``abort_job``.
"""

from __future__ import annotations

import json
import uuid

import pytest
from app.db.models import Task, User, Vacancy
from app.modules.queue_manager.queues import (
    LLM_JOB,
    LLM_QUEUE,
    PARSING_JOB,
    PARSING_QUEUE,
    acquire_recover_lock,
    enqueue_task,
    recover_pending_tasks,
)
from sqlalchemy import func

API = "/api/v1"
PASSWORD = "strongpassword"


# ============================================================
# Помощники
# ============================================================


@pytest.fixture
def factory(engine, client):
    """Sessionmaker тестовой БД (зависит от client — ради dependency override)."""
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    return async_sessionmaker(
        bind=engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
    )


async def _register_and_login(client, email: str | None = None) -> tuple[str, dict]:
    """Регистрация + подтверждение email → (user_id, auth-заголовки).

    Токены выдаёт POST /auth/verify-email: до подтверждения email вход
    запрещён (docs/03 §2).
    """
    from conftest import register_verified, user_id_for

    tokens = await register_verified(
        client, email or f"qr{uuid.uuid4().hex[:10]}@test.dev", PASSWORD
    )
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}
    return await user_id_for(client, headers), headers


async def _seed_task(
    factory,
    user_id: str,
    *,
    task_type: str = "parse_auto",
    status: str = "pending",
    payload: dict | None = None,
    **extra,
) -> str:
    async with factory() as session:
        task = Task(
            user_id=uuid.UUID(str(user_id)),
            task_type=task_type,
            status=status,
            progress_current=extra.pop("progress_current", 0),
            progress_total=extra.pop("progress_total", 3),
            payload=payload if payload is not None else {"keywords": ["python"]},
            **extra,
        )
        session.add(task)
        await session.commit()
        return str(task.id)


async def _get_task(factory, task_id: str) -> Task:
    async with factory() as session:
        return await session.get(Task, uuid.UUID(task_id))


def _events(pool) -> set[str]:
    """Имена событий, опубликованных в Realtime Module (docs/03 §8)."""
    return {json.loads(message)["event"] for message in pool.published}


def _job_ids(pool) -> list[str]:
    return [job["job_id"] for job in pool.jobs]


# ============================================================
# Отмена снимает job из очереди Redis (docs/03 §7, docs/04 §6)
# ============================================================


async def test_cancel_removes_job_from_redis_queue(client, factory, queue_pool):
    """POST /tasks/{id}/cancel снимает job из очереди Redis."""
    user_id, headers = await _register_and_login(client)
    task_id = await _seed_task(factory, user_id, task_type="parse_auto", status="pending")

    # Задача стоит в очереди (как это делает API при создании).
    await enqueue_task(task_id, "parse_auto", pool=queue_pool)
    assert len(queue_pool.jobs) == 1

    response = await client.post(f"{API}/tasks/{task_id}/cancel", headers=headers)
    assert response.status_code == 200, response.text
    assert response.json() == {
        "task_id": task_id,
        "status": "failed",
        "cancelled": True,
    }

    # Ключевое: job больше не в очереди — воркер его не подхватит.
    assert queue_pool.jobs == []
    assert _job_ids(queue_pool) == []


async def test_cancelled_task_never_executes(client, factory, queue_pool, queue_runner):
    """После отмены job отсутствует, поэтому прогонщик ничего не выполнит."""
    user_id, headers = await _register_and_login(client)
    task_id = await _seed_task(factory, user_id, task_type="parse_auto", status="pending")
    await enqueue_task(task_id, "parse_auto", pool=queue_pool)

    await client.post(f"{API}/tasks/{task_id}/cancel", headers=headers)
    await queue_runner.drain()  # не должно быть ничего для выполнения

    task = await _get_task(factory, task_id)
    assert task.status == "failed"
    assert task.result is None  # работа не выполнялась


async def test_cancel_publishes_realtime_events(client, factory, queue_pool):
    """Отмена публикует task.cancelled и task.failed для мгновенного UI."""
    user_id, headers = await _register_and_login(client)
    task_id = await _seed_task(factory, user_id, task_type="parse_auto")
    await enqueue_task(task_id, "parse_auto", pool=queue_pool)

    await client.post(f"{API}/tasks/{task_id}/cancel", headers=headers)
    assert {"task.cancelled", "task.failed"} <= _events(queue_pool)


async def test_cancel_writes_terminal_state_to_db(client, factory, queue_pool):
    """Отмена фиксирует failed + finished_at + понятный error_message."""
    user_id, headers = await _register_and_login(client)
    task_id = await _seed_task(factory, user_id, task_type="analyze", status="processing")

    await client.post(f"{API}/tasks/{task_id}/cancel", headers=headers)

    task = await _get_task(factory, task_id)
    assert task.status == "failed"
    assert task.error_message == "Отменено пользователем"
    assert task.finished_at is not None
    assert task.finished_at.tzinfo is not None  # timezone-aware (docs/02 §3.6)


async def test_cancel_is_final_and_idempotently_rejected(client, factory, queue_pool):
    """Повторная отмена отменённой задачи → 400 TASK_FAILED, а не двойной abort."""
    user_id, headers = await _register_and_login(client)
    task_id = await _seed_task(factory, user_id, task_type="parse_auto")
    await enqueue_task(task_id, "parse_auto", pool=queue_pool)

    first = await client.post(f"{API}/tasks/{task_id}/cancel", headers=headers)
    assert first.status_code == 200

    second = await client.post(f"{API}/tasks/{task_id}/cancel", headers=headers)
    assert second.status_code == 400
    assert second.json()["error_code"] == "TASK_FAILED"


async def test_cancel_blocked_for_terminal_and_applied(client, factory, queue_pool):
    """completed → 400; связанная вакансия applied → 409 (docs/02 §5)."""
    user_id, headers = await _register_and_login(client)

    done = await _seed_task(factory, user_id, task_type="parse_auto", status="completed")
    done_response = await client.post(f"{API}/tasks/{done}/cancel", headers=headers)
    assert done_response.status_code == 400
    assert done_response.json()["error_code"] == "TASK_COMPLETED"

    async with factory() as session:
        vacancy = Vacancy(
            user_id=uuid.UUID(user_id),
            hh_vacancy_id=uuid.uuid4().hex[:10],
            url="https://hh.ru/vacancy/1",
            status="applied",
            source="manual",
        )
        session.add(vacancy)
        await session.commit()
        vacancy_id = vacancy.id

    blocked = await _seed_task(
        factory, user_id, task_type="analyze", related_vacancy_id=vacancy_id
    )
    blocked_response = await client.post(f"{API}/tasks/{blocked}/cancel", headers=headers)
    assert blocked_response.status_code == 409
    assert blocked_response.json()["error_code"] == "VACANCY_APPLIED"


# ============================================================
# Восстановление после падения (docs/01 §3, docs/04 §6)
# ============================================================


async def _seed_recovery_task(
    factory,
    *,
    task_type: str = "parse_auto",
    status: str = "pending",
    payload: dict | None = None,
    **extra,
) -> str:
    """Создать пользователя с одной задачей (состояние «после падения»)."""
    async with factory() as session:
        user = User(email=f"rc{uuid.uuid4().hex[:10]}@test.dev", password_hash="x")
        session.add(user)
        await session.flush()
        task = Task(
            user_id=user.id,
            task_type=task_type,
            status=status,
            payload=payload if payload is not None else {"keywords": ["python"]},
            **extra,
        )
        session.add(task)
        await session.commit()
        return str(task.id)


async def test_recover_requeues_pending_task_after_crash(factory, queue_pool):
    """Задача, застрявшая в pending после падения, возвращается в очередь."""
    task_id = await _seed_recovery_task(factory, task_type="parse_manual", status="pending")

    restored = await recover_pending_tasks(factory, pool=queue_pool)

    assert restored == 1
    assert [job["args"][0] for job in queue_pool.jobs] == [task_id]
    assert queue_pool.jobs[0]["queue_name"] == PARSING_QUEUE
    assert queue_pool.jobs[0]["function"] == PARSING_JOB


async def test_recover_resets_interrupted_processing_task(factory, queue_pool):
    """``processing`` после падения → ``pending`` и возвращается в очередь.

    Работа не была финализирована, поэтому задача обязана быть перезапущена.
    """
    task_id = await _seed_recovery_task(
        factory, status="processing", progress_current=2, progress_total=5
    )

    restored = await recover_pending_tasks(factory, pool=queue_pool)
    assert restored == 1

    task = await _get_task(factory, task_id)
    assert task.status == "pending"
    assert task.started_at is None  # прежний старт сброшен
    assert task.finished_at is None  # задача не финализирована
    assert [job["args"][0] for job in queue_pool.jobs] == [task_id]


async def test_recover_preserves_payload_and_progress(factory, queue_pool):
    """Данные задачи не теряются: payload и прогресс сохраняются (docs/02 §3.6)."""
    payload = {
        "vacancy_ids": [str(uuid.uuid4()), str(uuid.uuid4())],
        "mode": "analyze_and_letter",
        "match_threshold": 80,
    }
    task_id = await _seed_recovery_task(
        factory,
        task_type="auto_full",
        status="processing",
        payload=payload,
        progress_current=1,
        progress_total=2,
    )

    await recover_pending_tasks(factory, pool=queue_pool)

    task = await _get_task(factory, task_id)
    # Ни один элемент payload не потерян и не обрезан.
    assert task.payload == payload
    assert task.progress_current == 1
    assert task.progress_total == 2
    # LLM-задача возвращается именно в LLM-очередь (docs/04 §6).
    assert queue_pool.jobs[0]["queue_name"] == LLM_QUEUE
    assert queue_pool.jobs[0]["function"] == LLM_JOB


async def test_recover_skips_terminal_tasks(factory, queue_pool):
    """``completed``/``failed`` не перезапускаются: результат уже окончателен."""
    for status in ("completed", "failed"):
        await _seed_recovery_task(factory, status=status)

    restored = await recover_pending_tasks(factory, pool=queue_pool)

    assert restored == 0
    assert queue_pool.jobs == []


async def test_recover_deduplicates_already_queued_tasks(factory, queue_pool):
    """Задача уже в очереди не ставится дважды: abort + повтор с суффиксом.

    Иначе после падения один пользователь получил бы N копий письма.
    """
    task_id = await _seed_recovery_task(factory, task_type="parse_manual", status="pending")

    # «Старый» job с тем же _job_id ещё жив в Redis.
    await queue_pool.enqueue_job(
        PARSING_JOB,
        task_id,
        _job_id=f"parse_manual:{task_id}",
        _queue_name=PARSING_QUEUE,
    )
    assert len(queue_pool.jobs) == 1

    restored = await recover_pending_tasks(factory, pool=queue_pool)

    assert restored == 1
    assert len(queue_pool.jobs) == 1  # старый снят, новый поставлен
    job_id = queue_pool.jobs[0]["job_id"]
    assert job_id.startswith(f"parse_manual:{task_id}:recover-")
    assert queue_pool.jobs[0]["args"] == (task_id,)


async def test_recover_handles_many_users_without_cross_contamination(factory, queue_pool):
    """Восстановление возвращает работу каждому своему пользователю."""
    expected = {await _seed_recovery_task(factory, status="processing") for _ in range(3)}

    restored = await recover_pending_tasks(factory, pool=queue_pool)
    assert restored == 3

    queued = {job["args"][0] for job in queue_pool.jobs}
    assert queued == expected  # ни одна задача не потеряна и не задвоена


async def test_recover_without_redis_is_noop(factory, monkeypatch):
    """Redis недоступен → восстановление молча ничего не делает (startup не падает)."""
    from app.modules.queue_manager import queues as queues_module

    async def _no_pool():
        return None

    monkeypatch.setattr(queues_module, "get_pool", _no_pool)
    await _seed_recovery_task(factory, status="pending")

    assert await recover_pending_tasks(factory) == 0


async def test_recover_lock_prevents_double_recovery(factory, queue_pool):
    """SET NX EX: вторая реплика пропускает восстановление (docs/01 §3).

    Иначе при рестарте pod'ов задачи ставились бы в очередь дважды.
    """
    task_id = await _seed_recovery_task(factory, status="pending")

    # Первая реплика захватила блокировку.
    assert await acquire_recover_lock(queue_pool) is True
    assert await acquire_recover_lock(queue_pool) is False

    restored = await recover_pending_tasks(factory, pool=queue_pool)

    assert restored == 0
    assert queue_pool.jobs == []
    task = await _get_task(factory, task_id)
    assert task.status == "pending"  # не тронута другим процессом


async def test_recover_lock_is_released_between_runs(factory, queue_pool):
    """Блокировка живёт по TTL: следующий запуск может восстановить очередь.

    Блокировку вручную не берём — ``recover_pending_tasks`` делает это сам.
    Проверяем, что после истечения TTL (пустое хранилище) захват снова
    успешен, то есть блокировка не «залипает» навсегда.
    """
    await _seed_recovery_task(factory, status="pending")

    # Блокировку заняли «мы» — восстановление пропускает работу.
    assert await acquire_recover_lock(queue_pool) is True
    await _seed_recovery_task(factory, status="processing")
    assert await recover_pending_tasks(factory, pool=queue_pool) == 0  # занято нами

    queue_pool._kv.clear()  # истёк TTL

    # Теперь обе задачи (pending + сброшенная в pending из processing)
    # возвращаются в очередь — ничего не потеряно.
    assert await recover_pending_tasks(factory, pool=queue_pool) == 2
    assert len(queue_pool.jobs) == 2


# ============================================================
# Честность восстановления: нет потерь и дублей (docs/04 §6)
# ============================================================


async def test_recovered_task_is_executable_after_crash(factory, queue_pool):
    """Восстановленная задача возвращается в очередь как исполняемая работа.

    Сквозной сценарий: падение (job потерян) → recover → job снова в очереди
    с правильными аргументами, чтобы воркер мог его выполнить.
    """
    task_id = await _seed_recovery_task(
        factory,
        task_type="parse_manual",
        status="processing",
        payload={"vacancy_url": "https://hh.ru/vacancy/1"},
    )

    # 1) Падение: задача была в processing, job потерян.
    assert queue_pool.jobs == []

    # 2) Восстановление возвращает её в очередь.
    await recover_pending_tasks(factory, pool=queue_pool)
    assert len(queue_pool.jobs) == 1

    job = queue_pool.jobs[0]
    assert job["args"] == (task_id,)
    assert job["function"] == PARSING_JOB
    assert job["queue_name"] == PARSING_QUEUE

    # 3) Задача снова готова к выполнению.
    task = await _get_task(factory, task_id)
    assert task.status == "pending"
    assert task.payload == {"vacancy_url": "https://hh.ru/vacancy/1"}


async def test_recovery_does_not_touch_completed_results(factory, queue_pool):
    """Восстановление не затирает результат уже завершённой задачи (docs/02 §3.6)."""
    async with factory() as session:
        user = User(email=f"rc10{uuid.uuid4().hex[:10]}@test.dev", password_hash="x")
        session.add(user)
        await session.flush()
        done = Task(
            user_id=user.id,
            task_type="analyze",
            status="completed",
            payload={"vacancy_ids": [str(uuid.uuid4())]},
            result={"vacancy_id": str(uuid.uuid4()), "status": "analyzed"},
        )
        lost = Task(
            user_id=user.id,
            task_type="parse_auto",
            status="pending",
            payload={"keywords": ["python"]},
        )
        session.add_all([done, lost])
        await session.commit()
        done_id = str(done.id)
        lost_id = str(lost.id)

    restored = await recover_pending_tasks(factory, pool=queue_pool)
    assert restored == 1

    completed = await _get_task(factory, done_id)
    assert completed.status == "completed"
    assert completed.result is not None  # результат на месте
    assert [job["args"][0] for job in queue_pool.jobs] == [lost_id]


async def test_task_counts_stay_consistent_after_cancel_and_recover(client, factory, queue_pool):
    """После отмены и восстановления число задач в БД и в очереди сходится."""
    from sqlalchemy import select as sa_select

    user_id, headers = await _register_and_login(client)

    cancelled = await _seed_task(factory, user_id, task_type="parse_auto")
    await enqueue_task(cancelled, "parse_auto", pool=queue_pool)
    await client.post(f"{API}/tasks/{cancelled}/cancel", headers=headers)

    for _ in range(2):
        await _seed_task(factory, user_id, task_type="parse_auto", status="processing")

    async with factory() as session:
        total = await session.scalar(sa_select(func.count()).select_from(Task))
    assert total == 3

    restored = await recover_pending_tasks(factory, pool=queue_pool)
    assert restored == 2

    async with factory() as session:
        pending = await session.scalar(
            sa_select(func.count()).select_from(Task).where(Task.status == "pending")
        )
    assert pending == 2  # отменённая (failed) не воскресла
    assert len(queue_pool.jobs) == 2