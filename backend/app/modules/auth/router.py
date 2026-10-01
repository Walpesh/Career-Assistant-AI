"""Auth Module — точка подключения роутера (docs/03_API_CONTRACTS.md §2).

Контракт:
    POST /auth/register — регистрация { email, password }
    POST /auth/login    — вход, выдача JWT
    POST /auth/refresh  — обновление access-токена по refresh-token
    GET  /auth/me       — текущий пользователь (Bearer JWT)

Таблицы: users (docs/02_DATABASE.md §3.1).
Бизнес-логика появится в следующих итерациях.
"""

from fastapi import APIRouter

router = APIRouter(prefix="/auth", tags=["auth"])
