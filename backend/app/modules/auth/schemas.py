"""Auth Module — Pydantic-схемы запросов/ответов (docs/03_API_CONTRACTS.md §2).

Тела запросов подтверждены в docs/03: { email, password } для register/login
и { refresh_token } для refresh. Требование к паролю (мин. 8 символов)
синхронизировано с валидацией на фронтенде (frontend/js/views/auth.js).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

__all__ = [
    "RegisterRequest",
    "LoginRequest",
    "RefreshRequest",
    "TokenResponse",
    "UserOut",
]


class _EmailPasswordBase(BaseModel):
    email: EmailStr = Field(max_length=255)
    password: str = Field(min_length=8, max_length=72)

    @field_validator("email")
    @classmethod
    def _normalize_email(cls, value: str) -> str:
        return value.strip().lower()


class RegisterRequest(_EmailPasswordBase):
    """POST /auth/register — { email, password } (docs/03 §2)."""


class LoginRequest(_EmailPasswordBase):
    """POST /auth/login — { email, password } (docs/03 §2)."""


class RefreshRequest(BaseModel):
    """POST /auth/refresh — body c refresh-token'ом."""

    refresh_token: str = Field(min_length=1)


class TokenResponse(BaseModel):
    """Ответ login/refresh: пара JWT (docs/03 §2, фронтенд ожидает access_token)."""

    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class UserOut(BaseModel):
    """Публичное представление users (docs/02 §3.1) для /auth/me и register."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: EmailStr
    is_active: bool
    created_at: datetime
