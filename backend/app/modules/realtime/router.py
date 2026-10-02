"""Realtime & Notification Module — WebSocket-канал (docs/03_API_CONTRACTS.md §8).

Подключение: /api/v1/ws?token=<access_token>

События сервер → клиент:
    task.created / task.progress / task.completed / task.failed
    vacancy.updated / analysis.ready / letter.ready / popup

Клиент на первом этапе только слушает (команды — через REST).
Событие popup показывается на фронтенде компонентом Fadeout-action-popup.

Протокол канала:
    - все сообщения сервера — валидный JSON вида {"event": ..., "data": {...}}
      (docs/03 §8); формируется только через json.dumps;
    - heartbeat: клиент шлёт {"event": "ping"}, сервер отвечает
      {"event": "pong"}; при отсутствии сообщений сервер сам шлёт pong
      каждые WS_HEARTBEAT_INTERVAL_SECONDS, чтобы прокси не рвали канал;
    - если соединение вытеснено более новым (вторая вкладка), сервер закрывает
      старое кодом WS_CLOSE_REPLACED — это штатная ситуация, а не обрыв сети.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from typing import Set

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState

from app.modules.auth.security import TOKEN_TYPE_ACCESS, TokenError, decode_token

router = APIRouter(tags=["realtime"])

logger = logging.getLogger(__name__)

# Track active WebSocket connections per user to prevent duplicates
# Key: user_id (str), Value: set of WebSocket connections
_active_connections: dict[str, Set[WebSocket]] = {}

# Lock for thread-safe access to connection tracking
_connection_lock = asyncio.Lock()

#: Соединение вытеснено более новым подключением того же пользователя.
WS_CLOSE_REPLACED = 4001

#: Интервал heartbeat по умолчанию, сек (client ping → server pong).
WS_HEARTBEAT_INTERVAL_SECONDS = 25.0


def encode_event(event: str, data: dict | None = None) -> str:
    """Единственная точка формирования сообщения канала (docs/03 §8).

    Раньше сообщение собиралось f-строкой, из-за чего payload dict
    сериализовался repr'ом с одинарными кавычками и JSON.parse на клиенте
    падал — события молча терялись.
    """
    return json.dumps({"event": event, "data": data or {}}, ensure_ascii=False)


def _is_ping(message: str) -> bool:
    try:
        parsed = json.loads(message)
    except (TypeError, ValueError):
        return False
    return isinstance(parsed, dict) and parsed.get("event") in ("ping", "ws.ping")


@router.websocket("/ws")
async def realtime_channel(websocket: WebSocket, token: str | None = None) -> None:
    """Канал реал-тайм событий с аутентификацией по JWT."""
    # Валидация токена (обязательна)
    if not token:
        await websocket.close(code=4401)  # 4401: Authentication error
        return

    try:
        payload = decode_token(token, expected_type=TOKEN_TYPE_ACCESS)
        user_id = str(payload.get("sub", ""))
    except TokenError:
        await websocket.close(code=4401)  # 4401: Authentication error
        return

    if not user_id:
        await websocket.close(code=4401)
        return

    # Регистрируем соединение и запоминаем предыдущие (дубликаты вкладок).
    async with _connection_lock:
        user_connections = _active_connections.setdefault(user_id, set())
        stale = [old_ws for old_ws in user_connections if old_ws is not websocket]
        user_connections.add(websocket)

    # Принимаем новое соединение
    await websocket.accept()

    for old_ws in stale:
        with contextlib.suppress(Exception):
            await old_ws.close(code=WS_CLOSE_REPLACED)

    try:
        # Держим соединение открытым; клиент шлёт ping, команды — через REST.
        while True:
            try:
                message = await asyncio.wait_for(
                    websocket.receive_text(), timeout=WS_HEARTBEAT_INTERVAL_SECONDS
                )
            except asyncio.TimeoutError:
                # Тишина клиента — подтверждаем, что канал жив (keepalive).
                with contextlib.suppress(Exception):
                    await websocket.send_text(encode_event("pong"))
                continue
            except WebSocketDisconnect:
                break
            except Exception:
                # Любая ошибка — закрываем соединение
                break

            if _is_ping(message):
                with contextlib.suppress(Exception):
                    await websocket.send_text(encode_event("pong"))
    finally:
        # Очистка соединения
        async with _connection_lock:
            user_connections.discard(websocket)
            if not user_connections:
                _active_connections.pop(user_id, None)


def get_active_connection_count() -> int:
    """Вспомогательная функция для тестирования. Возвращает общее количество соединений."""
    return sum(len(conns) for conns in _active_connections.values())


async def broadcast_to_user(user_id: str, event: str, data: dict) -> bool:
    """Отправить событие конкретному пользователю через WebSocket.

    Возвращает True, если хотя бы одно соединение получило сообщение.
    """
    message = encode_event(event, data)

    async with _connection_lock:
        connections = list(_active_connections.get(user_id, set()))

    if not connections:
        return False

    async def send_to_ws(ws: WebSocket) -> bool:
        try:
            if ws.client_state == WebSocketState.CONNECTED:
                await ws.send_text(message)
                return True
        except Exception:
            pass
        return False

    results = await asyncio.gather(
        *(send_to_ws(ws) for ws in connections), return_exceptions=True
    )

    # Удаляем соединения, которые не смогли принять сообщение (оборваны).
    async with _connection_lock:
        live = _active_connections.get(user_id)
        if live is not None:
            for ws, result in zip(connections, results):
                if result is not True:
                    live.discard(ws)
            if not live:
                _active_connections.pop(user_id, None)

    return any(result is True for result in results)
