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

from fastapi import APIRouter, WebSocket

router = APIRouter(tags=["realtime"])


@router.websocket("/ws")
async def realtime_channel(websocket: WebSocket, token: str | None = None) -> None:
    """Канал реал-тайм событий (заглушка точки подключения)."""
    await websocket.accept()
    await websocket.close(code=1000)
