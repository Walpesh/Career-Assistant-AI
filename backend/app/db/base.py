"""Declarative Base и общие миксины ORM-моделей (SQLAlchemy 2.0)."""

from datetime import datetime

from sqlalchemy import DateTime, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Базовый класс всех моделей — target_metadata для Alembic."""


class TimestampMixin:
    """created_at / updated_at (TIMESTAMPTZ, DEFAULT now()) — docs/02_DATABASE.md §1."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
