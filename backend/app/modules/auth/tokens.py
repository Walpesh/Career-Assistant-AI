"""Auth Module — серверный реестр refresh-токенов (rotation + reuse detection).

В БД хранится только SHA-256 хэш refresh-токена (см. app.db.models.RefreshToken).
Логика:

    issue   — создать refresh-JWT и записать его хэш как активную сессию;
    rotate  — при /auth/refresh: отозвать предъявленный токен и выдать новый;
    revoke  — при /auth/logout: отозвать конкретный токен.

Обнаружение повторного использования (reuse detection): если предъявлен
валидный по подписи refresh-JWT, который в реестре уже помечен revoked_at
(то есть был ротирован), значит токен утекал — все активные сессии
пользователя отзываются, а клиент получает 401.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.models import RefreshToken, User
from app.modules.auth.security import (
    TOKEN_TYPE_REFRESH,
    create_refresh_token,
    decode_token,
    hash_token,
)

__all__ = [
    "RefreshTokenError",
    "RefreshTokenReuseError",
    "issue_refresh_token",
    "rotate_refresh_token",
    "revoke_refresh_token",
    "revoke_all_user_tokens",
]


class RefreshTokenError(Exception):
    """Неизвестный/просроченный refresh-токен."""


class RefreshTokenReuseError(RefreshTokenError):
    """Токен уже был отозван — признак кражи (reuse)."""


def _now() -> datetime:
    return datetime.now(UTC)


async def issue_refresh_token(
    db: AsyncSession, user: User, *, user_agent: str | None = None
) -> str:
    """Создать refresh-JWT и сохранить его хэш как активную сессию."""
    token = create_refresh_token(user)
    payload = decode_token(token, expected_type=TOKEN_TYPE_REFRESH)
    expires_at = datetime.fromtimestamp(payload["exp"], tz=UTC)
    db.add(
        RefreshToken(
            user_id=user.id,
            jti=str(payload["jti"]),
            hashed_token=hash_token(token),
            user_agent=(user_agent or None),
            expires_at=expires_at,
        )
    )
    await db.flush()
    return token


async def rotate_refresh_token(
    db: AsyncSession, user: User, presented: str, *, user_agent: str | None = None
) -> str:
    """Ротировать refresh-токен; при повторном использовании — revoke all.

    Raises:
        RefreshTokenReuseError — токен уже отозван (reuse); все сессии отозваны.
        RefreshTokenError      — токен неизвестен или просрочен.
    """
    row = await db.scalar(
        select(RefreshToken).where(RefreshToken.hashed_token == hash_token(presented))
    )
    if row is None:
        raise RefreshTokenError("Refresh-токен не найден")

    if row.revoked_at is not None:
        if settings.refresh_token_reuse_detection:
            await revoke_all_user_tokens(db, user.id)
            raise RefreshTokenReuseError(
                "Обнаружено повторное использование refresh-токена; "
                "все сессии отозваны"
            )
        raise RefreshTokenError("Refresh-токен отозван")

    if row.expires_at <= _now():
        raise RefreshTokenError("Refresh-токен просрочен")

    row.revoked_at = _now()
    await db.flush()
    return await issue_refresh_token(db, user, user_agent=user_agent)


async def revoke_refresh_token(db: AsyncSession, presented: str) -> bool:
    """Отозвать конкретный refresh-токен (logout). True — если он был активен."""
    result = await db.execute(
        update(RefreshToken)
        .where(
            RefreshToken.hashed_token == hash_token(presented),
            RefreshToken.revoked_at.is_(None),
        )
        .values(revoked_at=_now())
    )
    return bool(result.rowcount)


async def revoke_all_user_tokens(db: AsyncSession, user_id) -> int:
    """Отозвать все активные refresh-токены пользователя."""
    result = await db.execute(
        update(RefreshToken)
        .where(
            RefreshToken.user_id == user_id,
            RefreshToken.revoked_at.is_(None),
        )
        .values(revoked_at=_now())
    )
    return int(result.rowcount or 0)
