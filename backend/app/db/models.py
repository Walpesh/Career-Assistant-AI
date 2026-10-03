"""SQLAlchemy 2.0 ORM-модели Career-Assistant-AI.

Схема строго соответствует docs/02_DATABASE.md (6 таблиц):
users, user_profiles, vacancies, analyses, cover_letters, tasks.

Дополнительно (согласовано): CHECK-ограничения по перечисленным в спецификации
значениям (vacancies.status/source, analyses.match_score, tasks.status,
user_profiles.match_threshold) и updated_at в tasks (общее правило §1).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin

__all__ = [
    "User",
    "UserProfile",
    "Vacancy",
    "Analysis",
    "CoverLetter",
    "Task",
    "RefreshToken",
]


class User(Base, TimestampMixin):
    """Аккаунты пользователей (docs/02_DATABASE.md §3.1)."""

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )
    email: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )

    profile: Mapped[UserProfile | None] = relationship(
        back_populates="user",
        uselist=False,
        cascade="all, delete-orphan",
    )
    vacancies: Mapped[list[Vacancy]] = relationship(back_populates="user")
    tasks: Mapped[list[Task]] = relationship(back_populates="user")
    refresh_tokens: Mapped[list[RefreshToken]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class UserProfile(Base, TimestampMixin):
    """Профиль, резюме и настройки матчинга (docs/02_DATABASE.md §3.2)."""

    __tablename__ = "user_profiles"
    __table_args__ = (
        CheckConstraint(
            "match_threshold >= 0 AND match_threshold <= 100",
            name="ck_user_profiles_match_threshold",
        ),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id"),
        primary_key=True,
    )
    full_name: Mapped[str | None] = mapped_column(String(255))
    resume_text: Mapped[str | None] = mapped_column(Text)
    compact_resume: Mapped[str | None] = mapped_column(Text)
    skills: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    experience_years: Mapped[Decimal | None] = mapped_column(Numeric(4, 1))
    desired_salary_from: Mapped[int | None] = mapped_column(Integer)
    desired_salary_to: Mapped[int | None] = mapped_column(Integer)
    match_threshold: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default=text("70")
    )
    preferred_work_formats: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    # Пожелания пользователя на человеческом языке («не хочу трудоустройство по
    # ТК РФ») — передаются в промпт анализа (docs/05 §4).
    analysis_preferences: Mapped[str | None] = mapped_column(Text)
    # Текст, который дописывается в конец сопроводительного письма «с красной
    # строки» скриптовым методом (docs/05 §5, §6).
    resume_addition: Mapped[str | None] = mapped_column(Text)

    user: Mapped[User] = relationship(back_populates="profile")


class Vacancy(Base, TimestampMixin):
    """Вакансии пользователя (docs/02_DATABASE.md §3.3, статусы §5)."""

    __tablename__ = "vacancies"
    __table_args__ = (
        CheckConstraint(
            "status IN ('raw', 'analyzed', 'letter_ready', 'applied', 'error')",
            name="ck_vacancies_status",
        ),
        CheckConstraint(
            "source IN ('auto', 'group', 'manual')",
            name="ck_vacancies_source",
        ),
        CheckConstraint(
            "match_score >= 0 AND match_score <= 100",
            name="ck_vacancies_match_score",
        ),
        # Уникальность вакансии на пользователя (docs/02 §1, §3.3)
        Index("uq_vacancies_user_hh_vacancy", "user_id", "hh_vacancy_id", unique=True),
        Index("ix_vacancies_user_status", "user_id", "status"),
        Index("ix_vacancies_user_created_at", "user_id", text("created_at DESC")),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    hh_vacancy_id: Mapped[str] = mapped_column(String(32), nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str | None] = mapped_column(String(512))
    company_name: Mapped[str | None] = mapped_column(String(512))
    salary_from: Mapped[int | None] = mapped_column(Integer)
    salary_to: Mapped[int | None] = mapped_column(Integer)
    salary_currency: Mapped[str | None] = mapped_column(
        String(8), server_default=text("'RUR'")
    )
    experience: Mapped[str | None] = mapped_column(String(64))
    employment_form: Mapped[str | None] = mapped_column(String(64))
    work_format: Mapped[str | None] = mapped_column(String(64))
    schedule: Mapped[str | None] = mapped_column(String(128))
    area: Mapped[str | None] = mapped_column(String(255))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    description_raw: Mapped[str | None] = mapped_column(Text)
    description_html: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=text("'raw'")
    )
    match_score: Mapped[int | None] = mapped_column(SmallInteger)
    source: Mapped[str | None] = mapped_column(String(32))

    user: Mapped[User] = relationship(back_populates="vacancies")
    analysis: Mapped[Analysis | None] = relationship(
        back_populates="vacancy",
        uselist=False,
        cascade="all, delete-orphan",
    )
    cover_letter: Mapped[CoverLetter | None] = relationship(
        back_populates="vacancy",
        uselist=False,
        cascade="all, delete-orphan",
    )
    tasks: Mapped[list[Task]] = relationship(back_populates="vacancy")


class Analysis(Base, TimestampMixin):
    """Результат анализа вакансии (docs/02_DATABASE.md §3.4)."""

    __tablename__ = "analyses"
    __table_args__ = (
        CheckConstraint(
            "match_score >= 0 AND match_score <= 100",
            name="ck_analyses_match_score",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    vacancy_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("vacancies.id"),
        nullable=False,
        unique=True,
    )
    match_score: Mapped[int | None] = mapped_column(SmallInteger)
    match_details: Mapped[dict | None] = mapped_column(JSONB)
    strengths: Mapped[str | None] = mapped_column(Text)
    weaknesses: Mapped[str | None] = mapped_column(Text)
    summary: Mapped[str | None] = mapped_column(Text)
    raw_llm_response: Mapped[dict | None] = mapped_column(JSONB)

    vacancy: Mapped[Vacancy] = relationship(back_populates="analysis")


class CoverLetter(Base, TimestampMixin):
    """Сгенерированное сопроводительное письмо (docs/02_DATABASE.md §3.5)."""

    __tablename__ = "cover_letters"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    vacancy_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("vacancies.id"),
        nullable=False,
        unique=True,
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("1")
    )
    raw_llm_response: Mapped[dict | None] = mapped_column(JSONB)

    vacancy: Mapped[Vacancy] = relationship(back_populates="cover_letter")


class Task(Base, TimestampMixin):
    """Очередь задач для отображения прогресса (docs/02_DATABASE.md §3.6, §6)."""

    __tablename__ = "tasks"
    __table_args__ = (
        CheckConstraint(
            # waiting_captcha — «пауза под ручное прохождение капчи» (docs/04 §2 п.3,
            # §5): задача не завершена, её возобновляет POST /tasks/{id}/resume.
            "status IN ('pending', 'processing', 'completed', 'failed', 'waiting_captcha')",
            name="ck_tasks_status",
        ),
        Index("ix_tasks_user_status", "user_id", "status"),
        Index("ix_tasks_status_created_at", "status", "created_at"),
        # Частичный индекс для воркеров (docs/02 §6)
        Index(
            "ix_tasks_status_pending",
            "status",
            postgresql_where=text("status = 'pending'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    task_type: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=text("'pending'")
    )
    progress_current: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    progress_total: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    #: Последний этап/сообщение прогресса — переживают перезагрузку страницы,
    #: пока фронтенд не получит новое событие task.progress (docs/04 §6).
    progress_stage: Mapped[str | None] = mapped_column(String(32))
    progress_message: Mapped[str | None] = mapped_column(Text)
    payload: Mapped[dict | None] = mapped_column(JSONB)
    result: Mapped[dict | None] = mapped_column(JSONB)
    error_message: Mapped[str | None] = mapped_column(Text)
    related_vacancy_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("vacancies.id")
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    user: Mapped[User] = relationship(back_populates="tasks")
    vacancy: Mapped[Vacancy | None] = relationship(back_populates="tasks")


class RefreshToken(Base, TimestampMixin):
    """Хранилище refresh-токенов: ротация и обнаружение повторного использования.

    Хранится только SHA-256 хэш токена (сам JWT в БД не пишется). Запись
    создаётся на login/refresh и помечается revoked_at при ротации или logout.
    Если предъявлен валидный JWT, которого нет среди активных хэшей (уже
    ротирован/отозван) — это признак кражи (reuse), и все активные токены
    пользователя отзываются.
    """

    __tablename__ = "refresh_tokens"
    __table_args__ = (
        Index("ix_refresh_tokens_user_id", "user_id"),
        Index("ix_refresh_tokens_jti", "jti", unique=True),
        Index("ix_refresh_tokens_hashed_token", "hashed_token", unique=True),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    jti: Mapped[str] = mapped_column(String(64), nullable=False)
    hashed_token: Mapped[str] = mapped_column(String(128), nullable=False)
    user_agent: Mapped[str | None] = mapped_column(String(512))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    user: Mapped[User] = relationship(back_populates="refresh_tokens")




