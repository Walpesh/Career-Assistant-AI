"""Жизненный цикл ``waiting_captcha`` и ``POST /tasks/{id}/resume``.

Сценарий «капча hh.ru» (docs/04 §2 п.3, §5; docs/03 §7):

    parsing → CaptchaDetected → waiting_captcha  (НЕ failed, задача не финализирована)
      → пользователь проходит капчу вручную в браузере
      → POST /tasks/{id}/resume → pending + job снова в Redis + task.resumed
      → воркер выполняет задачу заново

Критичные инварианты, которые проверяются:
    - ``waiting_captcha`` **не** терминальный статус: ``finished_at is None``;
    - ``waiting_captcha`` не перезапускается при ``recover_pending_tasks``;
    - resume доступен только из ``waiting_captcha`` (иначе 409);
    - resume возвращает задачу в **правильную** очередь (docs/04 §6);
    - при недоступной очереди статус откатывается в ``waiting_captcha``;
    - отмена ``waiting_captcha``-задачи работает (docs/03 §7).

Сеть, браузер и LLM не используются: оркестратор парсинга подменяется.
"""

from __future__ import annotations

import json
import uuid

import pytest
from app.db.models import Task, User
from app.modules.anti_ban.exceptions import CaptchaDetected
from app.modules.parsing import service as service_module
from app.modules.queue_manager.queues import (
    LLM_QUEUE,
    PARSING_QUEUE,
    QueueUnavailable,
    enqueue_task,
    queue_for_task_type,
    recover_pending_tasks,
)
from sqlalchemy import select

API = "/api/v1"
PASSWORD = "strongpassword"
WAITING = "waiting_captcha"


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

    target = email or f"cp{uuid.uuid4().hex[:10]}@test.dev"
    tokens = await register_verified(client, target, PASSWORD)
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}
    return await user_id_for(client, headers), headers


async def _seed_task(factory, user_id: str, *, status: str, task_type: str = "parse_auto") -> str:
    async with factory() as session:
        task = Task(
            user_id=uuid.UUID(str(user_id)),
            task_type=task_type,
            status=status,
            payload={"keywords": ["python"], "max_pages": 1},
        )
        session.add(task)
        await session.commit()
        return str(task.id)


async def _get_task(factory, task_id: str) -> Task:
    async with factory() as session:
        return await session.get(Task, uuid.UUID(task_id))


def _events(pool) -> set[str]:
    return {json.loads(message)["event"] for message in pool.published}


# ============================================================
# Переход в waiting_captcha (docs/04 §2 п.3, §5)
# ============================================================


async def test_captcha_moves_task_to_waiting_not_failed(factory, engine, queue_pool, monkeypatch):
    """CaptchaDetected → ``waiting_captcha``, а не ``failed`` (docs/04 §5)."""

    async def _captcha_auto(self, db, **kwargs):
        raise CaptchaDetected("Captcha detected")

    monkeypatch.setattr(service_module.ParsingOrchestrator, "run_auto", _captcha_auto)

    async with factory() as session:
        user = User(email=f"cp{uuid.uuid4().hex[:10]}@test.dev", password_hash="x")
        session.add(user)
        await session.flush()
        task = Task(
            user_id=user.id,
            task_type="parse_auto",
            status="pending",
            payload={"keywords": ["python"], "max_pages": 1},
        )
        session.add(task)
        await session.commit()
        task_id = task.id

    await enqueue_task(task_id, "parse_auto", pool=queue_pool)
    from app.modules.queue_manager.slots import InProcessQueueSlots
    from app.modules.queue_manager.worker import run_parsing_task

    ctx = {"session_factory": factory, "slots": InProcessQueueSlots()}
    await run_parsing_task(ctx, str(task_id))

    task = await _get_task(factory, str(task_id))
    assert task.status == WAITING
    # Задача НЕ финализирована: её вернёт /resume после ручного обхода капчи.
    assert task.finished_at is None
    assert "капча" in (task.error_message or "").lower()


async def test_waiting_captcha_is_not_recovered_by_crash_recovery(factory, queue_pool):
    """``waiting_captcha`` не перезапускается автоматически.

    Задача ждёт ручного вмешательства; авто-восстановление сняло бы её
    в бесконечный цикл капч (docs/04 §5).
    """
    async with factory() as session:
        user = User(email=f"cp{uuid.uuid4().hex[:10]}@test.dev", password_hash="x")
        session.add(user)
        await session.flush()
        task = Task(
            user_id=user.id, task_type="parse_auto", status=WAITING, payload={}
        )
        session.add(task)
        await session.commit()
        task_id = task.id

    restored = await recover_pending_tasks(factory, pool=queue_pool)

    assert restored == 0
    assert queue_pool.jobs == []
    assert (await _get_task(factory, str(task_id))).status == WAITING


# ============================================================
# POST /tasks/{id}/resume — возврат в очередь (docs/03 §7)
# ============================================================


async def test_resume_returns_task_to_queue(client, factory, queue_pool):
    """``waiting_captcha`` → ``pending`` + job в Redis + событие task.resumed."""
    user_id, headers = await _register_and_login(client)
    task_id = await _seed_task(factory, user_id, status=WAITING)
    await enqueue_task(task_id, "parse_auto", pool=queue_pool)
    queue_pool.jobs.clear()  # job был снят/утрачен — как после падения

    response = await client.post(f"{API}/tasks/{task_id}/resume", headers=headers)

    assert response.status_code == 200, response.text
    assert response.json() == {
        "task_id": task_id,
        "status": "pending",
        "resumed": True,
    }
    # Задача снова в очереди на исполнение.
    assert [job["args"][0] for job in queue_pool.jobs] == [task_id]
    assert queue_pool.jobs[0]["queue_name"] == PARSING_QUEUE
    assert "task.resumed" in _events(queue_pool)


async def test_resume_clears_error_and_timestamps(client, factory, queue_pool):
    """Resume очищает error_message и finished_at (docs/03 §7)."""
    user_id, headers = await _register_and_login(client)
    task_id = await _seed_task(factory, user_id, status=WAITING)

    async with factory() as session:
        task = await session.get(Task, uuid.UUID(task_id))
        task.error_message = "Обнаружена капча hh.ru"
        task.finished_at = None
        await session.commit()

    await client.post(f"{API}/tasks/{task_id}/resume", headers=headers)

    task = await _get_task(factory, task_id)
    assert task.status == "pending"
    assert task.error_message is None
    assert task.finished_at is None
    assert task.started_at is None


@pytest.mark.parametrize(
    "status",
    ["pending", "processing", "completed", "failed"],
)
async def test_resume_rejected_outside_waiting_captcha(client, factory, queue_pool, status):
    """Resume разрешён только из ``waiting_captcha`` (иначе 409) — docs/03 §7."""
    user_id, headers = await _register_and_login(client)
    task_id = await _seed_task(factory, user_id, status=status)

    response = await client.post(f"{API}/tasks/{task_id}/resume", headers=headers)

    assert response.status_code == 409, response.text
    assert response.json()["error_code"] == "TASK_NOT_WAITING_CAPTCHA"
    # Задача не переведена и в очередь не добавлена.
    assert queue_pool.jobs == []
    assert (await _get_task(factory, task_id)).status == status


async def test_resume_is_rejected_twice(client, factory, queue_pool):
    """Повторный resume после первого → 409: задача уже ``pending``."""
    user_id, headers = await _register_and_login(client)
    task_id = await _seed_task(factory, user_id, status=WAITING)

    first = await client.post(f"{API}/tasks/{task_id}/resume", headers=headers)
    assert first.status_code == 200

    second = await client.post(f"{API}/tasks/{task_id}/resume", headers=headers)
    assert second.status_code == 409
    assert second.json()["error_code"] == "TASK_NOT_WAITING_CAPTCHA"


async def test_resume_returns_task_to_correct_queue(client, factory, queue_pool):
    """LLM-задача после капчи возвращается именно в LLM-очередь (docs/04 §6)."""
    user_id, headers = await _register_and_login(client)
    task_id = await _seed_task(
        factory, user_id, status=WAITING, task_type="auto_full"
    )

    await client.post(f"{API}/tasks/{task_id}/resume", headers=headers)

    assert queue_pool.jobs[0]["queue_name"] == LLM_QUEUE
    assert queue_pool.jobs[0]["queue_name"] == queue_for_task_type("auto_full")


async def test_resume_rolls_back_when_queue_unavailable(client, factory, monkeypatch):
    """Недоступная очередь → 503, статус возвращается в ``waiting_captcha``.

    Иначе задача «потерялась» бы: в БД pending, а в Redis её нет.
    ``enqueue_task`` подменяется в модуле роутера: пакет ``queue_manager``
    реэкспортирует ``router`` как APIRouter, поэтому берём модуль через
    ``sys.modules`` — иначе подменялся бы не тот объект.
    """
    import sys

    import app.modules.queue_manager.router  # noqa: F401 — регистрирует модуль

    tasks_router = sys.modules["app.modules.queue_manager.router"]

    user_id, headers = await _register_and_login(client)
    task_id = await _seed_task(factory, user_id, status=WAITING)

    async def _unavailable(*args, **kwargs):
        raise QueueUnavailable("Redis недоступен")

    monkeypatch.setattr(tasks_router, "enqueue_task", _unavailable)

    response = await client.post(f"{API}/tasks/{task_id}/resume", headers=headers)

    assert response.status_code == 503
    assert response.json()["error_code"] == "QUEUE_UNAVAILABLE"

    task = await _get_task(factory, task_id)
    assert task.status == WAITING  # откат выполнен
    assert "Очередь задач недоступна" in task.error_message


# ============================================================
# Отмена waiting_captcha и полный цикл (docs/03 §7)
# ============================================================


async def test_waiting_captcha_task_can_be_cancelled(client, factory, queue_pool):
    """``waiting_captcha``-задачу можно отменить (docs/03 §7 — cancel доступен)."""
    user_id, headers = await _register_and_login(client)
    task_id = await _seed_task(factory, user_id, status=WAITING)
    await enqueue_task(task_id, "parse_auto", pool=queue_pool)

    response = await client.post(f"{API}/tasks/{task_id}/cancel", headers=headers)

    assert response.status_code == 200, response.text
    assert response.json()["cancelled"] is True
    assert queue_pool.jobs == []

    task = await _get_task(factory, task_id)
    assert task.status == "failed"
    assert task.finished_at is not None


async def test_full_captcha_cycle(client, factory, queue_pool, monkeypatch):
    """Полный цикл: капча → resume → успешное выполнение (docs/04 §2 п.3).

    Воркер на втором проходе уже не встречает капчу и доводит задачу до конца.
    """
    from app.modules.queue_manager.slots import InProcessQueueSlots
    from app.modules.queue_manager.worker import run_parsing_task

    user_id, headers = await _register_and_login(client)
    task_id = await _seed_task(factory, user_id, status=WAITING)

    # 1) Пользователь прошёл капчу и нажал «возобновить».
    resumed = await client.post(f"{API}/tasks/{task_id}/resume", headers=headers)
    assert resumed.status_code == 200, resumed.text
    assert len(queue_pool.jobs) == 1

    # 2) Воркер выполняет задачу без капчи.
    async def _ok_auto(self, db, **kwargs):
        from app.modules.parsing.service import ParsingOutcome

        return ParsingOutcome(vacancy_ids=[], failed=0)

    monkeypatch.setattr(service_module.ParsingOrchestrator, "run_auto", _ok_auto)

    ctx = {"session_factory": factory, "slots": InProcessQueueSlots()}
    await run_parsing_task(ctx, task_id)

    task = await _get_task(factory, task_id)
    assert task.status == "completed"
    assert task.finished_at is not None


async def test_task_endpoint_exposes_waiting_captcha_state(client, factory):
    """GET /tasks/{id} отдаёт статус ``waiting_captcha`` (docs/03 §7)."""
    user_id, headers = await _register_and_login(client)
    task_id = await _seed_task(factory, user_id, status=WAITING)

    response = await client.get(f"{API}/tasks/{task_id}", headers=headers)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == WAITING
    assert body["finished_at"] is None


async def test_waiting_captcha_appears_in_task_list(client, factory):
    """Задача в ``waiting_captcha`` видна в списке с пагинацией (docs/03 §7)."""
    user_id, headers = await _register_and_login(client)
    task_id = await _seed_task(factory, user_id, status=WAITING)

    response = await client.get(f"{API}/tasks", headers=headers)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["id"] == task_id
    assert body["items"][0]["status"] == WAITING


async def test_waiting_captcha_does_not_consume_llm_queue(client, factory, queue_pool):
    """Задача, ждущая капчи, не занимает единственный слот LLM (docs/05 §1).

    Проверяется на уровне маршрутизации: парсинговая задача после капчи
    ждёт в ``waiting_captcha`` и не занимает LLM-очередь.
    """
    user_id, headers = await _register_and_login(client)
    task_id = await _seed_task(factory, user_id, status=WAITING, task_type="parse_auto")

    await client.post(f"{API}/tasks/{task_id}/resume", headers=headers)

    assert queue_pool.jobs[0]["queue_name"] == PARSING_QUEUE
    assert queue_pool.jobs[0]["queue_name"] != LLM_QUEUE


async def test_unknown_task_resume_is_404(client, factory, queue_pool):
    """Resume несуществующей задачи → 404, а не 409 (docs/03 §7)."""
    _, headers = await _register_and_login(client)

    response = await client.post(f"{API}/tasks/{uuid.uuid4()}/resume", headers=headers)

    assert response.status_code == 404
    assert response.json()["error_code"] == "NOT_FOUND"
    assert queue_pool.jobs == []


async def test_resume_requires_authentication(client, factory):
    """Анонимный resume → 401: задача не возвращается в очередь (docs/03 §1)."""
    user_id, _ = await _register_and_login(client)
    task_id = await _seed_task(factory, user_id, status=WAITING)

    response = await client.post(f"{API}/tasks/{task_id}/resume")

    assert response.status_code == 401
    assert (await _get_task(factory, task_id)).status == WAITING


async def test_select_only_returns_captcha_tasks(factory):
    """В выборке БД ``waiting_captcha`` — обычный статус (docs/02 §3.6)."""
    async with factory() as session:
        user = User(email=f"cpq{uuid.uuid4().hex[:10]}@test.dev", password_hash="x")
        session.add(user)
        await session.flush()
        session.add(Task(user_id=user.id, task_type="parse_auto", status=WAITING, payload={}))
        session.add(Task(user_id=user.id, task_type="parse_auto", status="pending", payload={}))
        await session.commit()

        rows = list((await session.scalars(select(Task))).all())

    statuses = [row.status for row in rows]
    assert statuses.count(WAITING) == 1
    assert len(rows) == 2