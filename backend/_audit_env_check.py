"""Проверка доступности WS/HTTP-клиентов для live-аудита (временный скрипт)."""
import importlib.util as u

for name in ("websockets", "wsproto", "httpx", "asyncpg", "starlette"):
    print(name, bool(u.find_spec(name)))
