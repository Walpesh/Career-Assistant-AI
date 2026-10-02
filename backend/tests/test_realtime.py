"""Тесты Realtime Module — WebSocket-канал (docs/03_API_CONTRACTS.md §8).

Покрывают корневые причины «сломанного реал-тайма»:
    - сообщение канала обязано быть валидным JSON (раньше payload собирался
      f-строкой, dict сериализовался repr'ом с одинарными кавычками, и
      JSON.parse на клиенте падал — события молча терялись);
    - broadcast доставляет событие всем соединениям пользователя;
    - оборванные соединения вычищаются из реестра.
"""

from __future__ import annotations

import importlib
import json

import pytest
from starlette.websockets import WebSocketDisconnect, WebSocketState

# Пакет app.modules.realtime экспортирует APIRouter под именем `router`,
# поэтому модуль берём через importlib (как в test_auth_profile.py).
realtime = importlib.import_module("app.modules.realtime.router")


class FakeWebSocket:
    """Минимальная заглушка WebSocket (client_state + send_text/close)."""

    def __init__(self, *, connected: bool = True, fail_on_send: bool = False) -> None:
        self.client_state = WebSocketState.CONNECTED if connected else WebSocketState.DISCONNECTED
        self.fail_on_send = fail_on_send
        self.sent: list[str] = []
        self.closed_with: int | None = None

    async def send_text(self, text: str) -> None:
        if self.fail_on_send:
            raise RuntimeError("connection lost")
        self.sent.append(text)

    async def close(self, code: int = 1000) -> None:
        self.closed_with = code
        self.client_state = WebSocketState.DISCONNECTED


@pytest.fixture(autouse=True)
def _clean_connections():
    """Реестр соединений — глобальный состояние модуля; чистим между тестами."""
    realtime._active_connections.clear()
    yield
    realtime._active_connections.clear()


# ---------------------------------------------------------------- формат


def test_encode_event_produces_valid_json():
    """docs/03 §8: { event, data } — валидный JSON с данными-объектом."""
    raw = realtime.encode_event(
        "task.progress",
        {"task_id": "abc", "current": 12, "total": 47, "message": "Парсинг вакансии 12/47"},
    )

    parsed = json.loads(raw)  # раньше здесь был SyntaxError на клиенте
    assert parsed["event"] == "task.progress"
    assert parsed["data"]["task_id"] == "abc"
    assert parsed["data"]["current"] == 12
    assert "Парсинг" in parsed["data"]["message"]  # unicode не экранируется мусором

    # Пустой payload тоже остаётся объектом.
    assert json.loads(realtime.encode_event("pong"))["data"] == {}


# ---------------------------------------------------------------- broadcast


async def test_broadcast_to_user_delivers_parsable_event():
    """Событие доходит до соединения пользователя и читается клиентом."""
    user_id = "user-1"
    socket = FakeWebSocket()
    realtime._active_connections[user_id] = {socket}

    delivered = await realtime.broadcast_to_user(
        user_id, "task.completed", {"task_id": "t-1", "result": {"created": 2}}
    )

    assert delivered is True
    assert len(socket.sent) == 1

    payload = json.loads(socket.sent[0])
    assert payload["event"] == "task.completed"
    assert payload["data"]["result"]["created"] == 2


async def test_broadcast_to_unknown_user_is_noop():
    """Нет соединений — False, без исключений (docs/03 §8 — доставка best-effort)."""
    assert await realtime.broadcast_to_user("nobody", "popup", {"title": "hi"}) is False


async def test_broadcast_skips_not_connected_socket():
    """Уже закрытое соединение не считается доставкой."""
    user_id = "user-2"
    realtime._active_connections[user_id] = {FakeWebSocket(connected=False)}

    assert await realtime.broadcast_to_user(user_id, "popup", {"title": "hi"}) is False


async def test_broadcast_drops_broken_connection_from_registry():
    """Соединение, не принявшее сообщение, удаляется из реестра."""
    user_id = "user-3"
    broken = FakeWebSocket(fail_on_send=True)
    alive = FakeWebSocket()
    realtime._active_connections[user_id] = {broken, alive}

    delivered = await realtime.broadcast_to_user(user_id, "popup", {"title": "hi"})

    assert delivered is True
    assert realtime._active_connections[user_id] == {alive}
    assert realtime.get_active_connection_count() == 1


# ---------------------------------------------------------------- канал


def _access_token() -> str:
    """JWT для несуществующего пользователя: канал БД не использует."""
    import uuid

    from app.modules.auth.security import create_access_token

    class _User:
        id = uuid.uuid4()
        email = "ws@test.dev"

    return create_access_token(_User())


def _ws_client():
    """TestClient без lifespan — воркер очереди для проверки канала не нужен."""
    from fastapi.testclient import TestClient

    from app.main import app

    return TestClient(app)


def test_ws_channel_answers_ping_with_parsable_json():
    """Клиентский ping → серверный pong в валидном JSON (docs/03 §8)."""
    client = _ws_client()
    with client.websocket_connect(f"/api/v1/ws?token={_access_token()}") as ws:
        ws.send_text(json.dumps({"event": "ping"}))
        message = ws.receive_json()  # невалидный JSON ронял бы клиент

    assert message["event"] == "pong"
    assert message["data"] == {}


def test_new_connection_replaces_previous_one():
    """Дубликат подключения вытесняет старое кодом WS_CLOSE_REPLACED.

    Клиент на этот код не переподключается — иначе сервер и клиент
    вытесняли бы друг друга бесконечно (ping-pong).
    """
    client = _ws_client()
    url = f"/api/v1/ws?token={_access_token()}"

    with client.websocket_connect(url) as first:
        with client.websocket_connect(url) as second:
            closed = first.receive()
            assert closed["type"] == "websocket.close"
            assert closed.get("code") == realtime.WS_CLOSE_REPLACED

            # Новое соединение живо и отвечает на heartbeat.
            second.send_text(json.dumps({"event": "ping"}))
            assert second.receive_json()["event"] == "pong"


def test_ws_rejects_request_without_token():
    """Без токена канал закрывается кодом 4401 (docs/03 §2)."""
    client = _ws_client()
    with pytest.raises(WebSocketDisconnect) as excinfo:
        with client.websocket_connect("/api/v1/ws") as ws:
            ws.receive_text()

    assert excinfo.value.code == 4401