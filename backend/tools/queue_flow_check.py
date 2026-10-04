"""Проверка сквозного потока: API → Redis/ARQ → воркер → БД → WebSocket.

Запуск (сервер должен быть уже поднят на порту из переменной CA_PORT):

    cd backend
    python tools/queue_flow_check.py

Скрипт проверяет живую интеграцию, которую не покрывают юнит-тесты:
    1. POST /parsing/manual кладёт задачу в очередь Redis (enqueue_job);
    2. ARQ-воркер забирает её и пишет started_at / finished_at / прогресс в БД;
    3. события task.* приходят в WebSocket в корректном JSON (docs/03 §8).

Сеть hh.ru используется по-настоящему, поэтому фактический результат парсинга
зависит от доступности сайта; проверяется именно работа очереди.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid
from pathlib import Path

#: Корень backend в sys.path — чтобы работали импорты app.* (настройки, Redis).
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx
import websockets

PORT = os.environ.get("CA_PORT", "8099")
BASE_URL = f"http://127.0.0.1:{PORT}"
WS_URL = f"ws://127.0.0.1:{PORT}/api/v1/ws"

PASSWORD = "pass12345"
VACANCY_URL = "https://hh.ru/vacancy/137866214"

#: OTP-код подтверждения email (docs/03 §2). Обязателен: скрипт работает
#: против живого сервера, а код приходит письмом (или пишется в лог, если
#: SMTP не настроен). Пример запуска:
#:     CA_OTP_CODE=123456 python -m tools.queue_flow_check
OTP_CODE = os.environ.get("CA_OTP_CODE", "")

#: Сколько секунд ждать завершения задачи и событий.
TASK_TIMEOUT = 120.0


def log(message: str) -> None:
    print(f"[queue-flow] {message}", flush=True)


async def register_and_login(client: httpx.AsyncClient, code: str | None = None) -> str:
    """Регистрация + подтверждение email → access_token (docs/03 §2).

    Токены выдаёт POST /auth/verify-email: до подтверждения email вход
    запрещён (403 EMAIL_NOT_VERIFIED). ``code`` обязателен — скрипт идёт
    против живого сервера, поэтому код берётся из письма или из лога
    (в dev-режиме без SMTP код пишется в лог приложения).
    """
    payload = {"email": f"qflow{uuid.uuid4().hex[:10]}@test.dev", "password": PASSWORD}
    registered = await client.post("/api/v1/auth/register", json=payload)
    if registered.status_code not in (200, 201):
        raise RuntimeError(f"register failed: {registered.status_code} {registered.text}")
    if not code:
        raise RuntimeError(
            "нужен OTP-код: проверка идёт против живого сервера — возьмите "
            "6 цифр из письма или из лога приложения"
        )
    verified = await client.post(
        "/api/v1/auth/verify-email", json={"email": payload["email"], "code": code}
    )
    verified.raise_for_status()
    return verified.json()["access_token"]


async def collect_ws_events(token: str, task_id: str, events: list[dict]) -> None:
    """Слушать события задачи, пока не придёт финальное или не выйдет таймаут."""
    url = f"{WS_URL}?token={token}"
    deadline = asyncio.get_event_loop().time() + TASK_TIMEOUT
    async with websockets.connect(url) as ws:
        while asyncio.get_event_loop().time() < deadline:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=10.0)
            except asyncio.TimeoutError:
                continue
            try:
                message = json.loads(raw)
            except (TypeError, ValueError):
                log(f"НЕВАЛИДНЫЙ JSON в канале: {raw!r}")
                continue
            events.append(message)
            data = message.get("data") or {}
            if data.get("task_id") == task_id and message.get("event") in (
                "task.completed",
                "task.failed",
            ):
                return


async def wait_for_task(token: str, task_id: str) -> dict:
    """Дождаться, пока задача перестанет быть pending/processing."""
    deadline = asyncio.get_event_loop().time() + TASK_TIMEOUT
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=30.0) as client:
        while asyncio.get_event_loop().time() < deadline:
            response = await client.get(
                f"/api/v1/tasks/{task_id}",
                headers={"Authorization": f"Bearer {token}"},
            )
            response.raise_for_status()
            task = response.json()
            if task["status"] in ("completed", "failed"):
                return task
            await asyncio.sleep(2.0)
    raise RuntimeError(f"задача {task_id} не завершилась за {TASK_TIMEOUT} с")


def describe_queues(task_id: str) -> str:
    """Показать, где именно лежит job (если Redis доступен)."""
    try:
        import redis

        from app.core.config import settings

        client = redis.Redis.from_url(settings.redis_url)
        job_key = f"arq:job:parse_manual:{task_id}"
        exists = client.exists(job_key)
        queues = {
            name: client.type(name).decode()
            for name in (settings.arq_queue_parsing, settings.arq_queue_llm)
            if client.exists(name)
        }
        return f"job_key_exists={bool(exists)}, типы ключей очередей={queues}"
    except Exception as exc:  # noqa: BLE001 — Redis не обязателен для проверки
        return f"не удалось прочитать Redis: {exc}"


async def main() -> int:
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=30.0) as client:
        token = await register_and_login(client, OTP_CODE)
        log("пользователь зарегистрирован, email подтверждён, токен получен")

        # --- задача уходит в очередь Redis -------------------------------
        response = await client.post(
            "/api/v1/parsing/manual",
            json={"vacancy_url": VACANCY_URL, "run_analysis": False},
            headers={"Authorization": f"Bearer {token}"},
        )
        response.raise_for_status()
        body = response.json()

    task_id = body["task_id"]
    log(f"задача создана: task_id={task_id} status={body['status']}")
    log(f"job в очереди: {describe_queues(task_id)}")

    # --- события WebSocket + итоговый статус ----------------------------
    events: list[dict] = []
    collector = asyncio.create_task(collect_ws_events(token, task_id, events))
    await asyncio.sleep(1)  # даём воркеру стартовать

    final = await wait_for_task(token, task_id)
    await collector

    names = [event.get("event") for event in events]
    log(f"события WebSocket: {names}")
    log(
        f"итог задачи: status={final['status']} "
        f"progress={final['progress_current']}/{final['progress_total']} "
        f"started_at={final['started_at']} finished_at={final['finished_at']} "
        f"error={final['error_message']}"
    )

    failures: list[str] = []
    if final["status"] not in ("completed", "failed"):
        failures.append(f"задача не финализирована: {final['status']}")
    if final["status"] == "completed" and not final["started_at"]:
        failures.append("started_at не заполнен воркером")
    if final["status"] == "completed" and not final["finished_at"]:
        failures.append("finished_at не заполнен воркером")
    if not names:
        failures.append("WebSocket не отдал ни одного события")
    if not any(name in ("task.created", "task.started", "task.progress") for name in names):
        failures.append(f"нет событий о ходе задачи: {names}")

    if failures:
        for item in failures:
            log(f"ПРОВАЛ: {item}")
        return 1

    log("OK: задача поставлена в Redis, выполнена воркером, события доставлены")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
