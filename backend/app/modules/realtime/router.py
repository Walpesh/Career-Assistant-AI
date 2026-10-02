"""Realtime & Notification Module — WebSocket-канал (docs/03_API_CONTRACTS.md §8).

Подключение: /api/v1/ws?token=<access_token>

События сервер → клиент:
    task.created / task.progress / task.completed / task.failed
    vacancy.updated / analysis.ready / letter.ready / popup

Клиент на первом этапе только слушает (команды — через REST).
Событие popup показывается на фронтенде компонентом Fadeout-action-popup.

TODO: аутентификация по token и трансляция событий из Realtime Module
(Redis pub/sub между воркерами и API-процессом).
"""

from __future__ import annotations

import asyncio
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

    # Подключаем к соединениям пользователя
    user_connections: Set[WebSocket] = set()
    async with _connection_lock:
        if user_id not in _active_connections:
            _active_connections[user_id] = set()
        user_connections = _active_connections[user_id]

    # Проверяем, есть ли уже активное соединение (дубликат)
    if len(user_connections) > 0:
        # Закрываем старые соединения
        for old_ws in user_connections:
            if old_ws.client_state == WebSocketState.CONNECTED:
                try:
                    await old_ws.close(code=1000)
                except Exception:
                    pass
            async with _connection_lock:
                user_connections.discard(old_ws)

    # Принимаем новое соединение
    await websocket.accept()
    user_connections.add(websocket)

    try:
        # Держим соединение открытым и ждём сообщений от клиента
        while websocket.client_state == WebSocketState.CONNECTED:
            try:
                message = await websocket.receive_text()
                # Клиент может отправлять ping/other messages
                # В данный момент сервер только читает
            except WebSocketDisconnect:
                break
            except Exception:
                # Любая ошибка — закрываем соединение
                break
    finally:
        # Очистка соединения
        user_connections.discard(websocket)
        if not user_connections:
            async with _connection_lock:
                _active_connections.pop(user_id, None)


def get_active_connection_count() -> int:
    """Вспомогательная функция для тестирования. Возвращает общее количество соединений."""
    return sum(len(conns) for conns in _active_connections.values())


async def broadcast_to_user(user_id: str, event: str, data: dict) -> bool:
    """Отправить событие конкретному пользователю через WebSocket.

    Возвращает True, если хотя бы одно соединение получило сообщение.
    """
    async with _connection_lock:
        connections = _active_connections.get(user_id, set()).copy()

    if not connections:
        return False

    message = f'{{"event": "{event}", "data": {data}}}'

    async def send_to_ws(ws: WebSocket) -> bool:
        try:
            if ws.client_state == WebSocketState.CONNECTED:
                await ws.send_text(message)
                return True
        except Exception:
            pass
        return False

    results = await asyncio.gather(*[send_to_ws(ws) for ws in connections])
    return any(results)
