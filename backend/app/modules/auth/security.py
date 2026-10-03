"""Auth Module — криптография: bcrypt-хэши паролей и JWT (docs/03 §2).

JWT payload (docs/03 фиксирует только Bearer-схему и refresh-поток, claims —
стандартная структура JWT):

    access:  { "sub": "<user_id UUID>", "type": "access",  "email": "...", "iat", "exp" }
    refresh: { "sub": "<user_id UUID>", "type": "refresh", "email": "...", "iat", "exp" }

- HS256, секрет/сроки — из app.core.config (JWT_SECRET, ACCESS_TOKEN_EXPIRE_MINUTES,
  REFRESH_TOKEN_EXPIRE_DAYS; см. backend/.env.example).
- Пароли: bcrypt (cost 12), ограничение алгоритма в 72 байта учтено.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from uuid import uuid4

import bcrypt
from jose import JWTError, jwt

from app.core.config import settings

if TYPE_CHECKING:
    from app.db.models import User

__all__ = [
    "hash_password",
    "verify_password",
    "create_access_token",
    "create_refresh_token",
    "decode_token",
    "hash_token",
    "TokenError",
    "TOKEN_TYPE_ACCESS",
    "TOKEN_TYPE_REFRESH",
]

TOKEN_TYPE_ACCESS = "access"
TOKEN_TYPE_REFRESH = "refresh"

# bcrypt физически ограничение 72 байтами — режем до лимита (стандартная практика).
_BCRYPT_MAX_BYTES = 72


class TokenError(Exception):
    """Невалидный, просроченный или чужой по типу токен."""


def _to_bytes(password: str) -> bytes:
    return password.encode("utf-8")[:_BCRYPT_MAX_BYTES]


def hash_password(password: str) -> str:
    """Хэш пароля bcrypt (salt встроен в результат)."""
    return bcrypt.hashpw(_to_bytes(password), bcrypt.gensalt(rounds=12)).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    """Проверка пароля against bcrypt-хэша (невалидный хэш → False)."""
    try:
        return bcrypt.checkpw(_to_bytes(password), password_hash.encode("utf-8"))
    except (ValueError, TypeError):
        return False


def _create_token(user: "User", token_type: str, expire_delta: timedelta) -> str:
    now = datetime.now(UTC)
    payload = {
        "sub": str(user.id),
        "type": token_type,
        "email": user.email,
        "jti": str(uuid4()),  # уникальность: каждый токен — новая ротация
        "iat": now,
        "exp": now + expire_delta,
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def create_access_token(user: "User") -> str:
    """Access-токен (короткий срок жизни)."""
    return _create_token(
        user, TOKEN_TYPE_ACCESS, timedelta(minutes=settings.access_token_expire_minutes)
    )


def create_refresh_token(user: "User") -> str:
    """Refresh-токен (длительный срок жизни)."""
    return _create_token(user, TOKEN_TYPE_REFRESH, timedelta(days=settings.refresh_token_expire_days))


def decode_token(token: str, expected_type: str) -> dict:
    """Декодирование и валидация JWT; бросает TokenError при любой проблеме."""
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    except JWTError as exc:  # ExpiredSignatureError — тоже здесь
        raise TokenError("Невалидный или просроченный токен") from exc

    if payload.get("type") != expected_type:
        raise TokenError("Недопустимый тип токена")
    if not payload.get("sub"):
        raise TokenError("В токене отсутствует subject (user id)")
    return payload


def hash_token(token: str) -> str:
    """SHA-256 хэш токена (hex) — в БД хранится только он, не сам JWT."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
