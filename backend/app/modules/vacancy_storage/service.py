"""CRUD-сервис Vacancy Storage Module (docs/01 §3, docs/02 §3.3/§5, docs/04 §8).

Ответственности модуля: сохранение, дедупликация, обновление статусов.

Дедупликация (docs/02 §1, docs/04 §8), ключ (user_id, hh_vacancy_id):
    - строки нет            → создание с переданным статусом (raw или error);
    - есть, status='applied' → поля НЕ перезаписываются (applied неизменяем);
    - есть, иначе            → обновление контент-полей; статус:
        * ingest_status='error' (вакансия не найдена на hh) → 'error';
        * текущий 'error', парсинг прошёл                   → восстановление 'raw';
        * 'analyzed'/'letter_ready' сохраняются — без отката прогресса
          (по docs/04 §8 данные для них обновляются только вместе с новым
          анализом и письмом).

Граф статусов (docs/02 §5) — ALLOWED_TRANSITIONS; `applied` терминален:
ручной PATCH с него отклоняется с 409.
"""

from __future__ import annotations

import re
import uuid
from typing import Sequence

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.db.models import Vacancy

__all__ = [
    "VACANCY_STATUSES",
    "ALLOWED_TRANSITIONS",
    "extract_hh_vacancy_id",
    "get_user_vacancy",
    "upsert_vacancy",
    "list_user_vacancies",
    "change_status",
    "delete_vacancy",
]

VACANCY_STATUSES: tuple[str, ...] = (
    "raw",
    "analyzed",
    "letter_ready",
    "applied",
    "error",
)

# Целостность жизненного цикла: raw → анализ → письмо → отклик; error —
# точка восстановления после неудачного парсинга/анализа (docs/04 §5).
ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    "raw": frozenset({"analyzed", "error", "applied"}),
    "analyzed": frozenset({"letter_ready", "error", "applied"}),
    "letter_ready": frozenset({"applied", "error"}),
    "applied": frozenset(),  # терминальный: не перезаписывается (docs/04 §8)
    "error": frozenset({"raw", "analyzed", "letter_ready", "applied"}),
}

# Контент-поля, обновляемые при дедупликации (docs/04 §7 — что сохраняется).
_CONTENT_FIELDS: tuple[str, ...] = (
    "title",
    "company_name",
    "salary_from",
    "salary_to",
    "salary_currency",
    "experience",
    "employment_form",
    "work_format",
    "schedule",
    "area",
    "published_at",
    "description_raw",
    "description_html",
)

# Прямая ссылка https://<поддомен>.hh.ru/vacancy/<digits> (+ query/fragment).
_HH_VACANCY_URL_RE = re.compile(
    r"^https?://(?:[a-z0-9-]+\.)*hh\.ru/vacancy/(\d{1,12})(?:[/?#].*)?$",
    re.IGNORECASE,
)


def extract_hh_vacancy_id(vacancy_url: str) -> str:
    """hh_vacancy_id из прямой ссылки; иначе 400 INVALID_VACANCY_URL."""
    match = _HH_VACANCY_URL_RE.match(vacancy_url.strip())
    if match is None:
        raise AppError(
            400,
            "Ожидается прямая ссылка на вакансию вида "
            "https://<город>.hh.ru/vacancy/<id>",
            "INVALID_VACANCY_URL",
        )
    return match.group(1)


async def get_user_vacancy(
    db: AsyncSession, user_id: uuid.UUID, vacancy_id: uuid.UUID
) -> Vacancy:
    """Вакансия текущего пользователя; чужие/неизвестные → 404 (без утечки)."""
    vacancy = await db.get(Vacancy, vacancy_id)
    if vacancy is None or vacancy.user_id != user_id:
        raise AppError(404, "Вакансия не найдена", "NOT_FOUND")
    return vacancy


async def upsert_vacancy(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    hh_vacancy_id: str,
    url: str,
    source: str,
    fields: dict[str, object],
    ingest_status: str = "raw",
) -> tuple[Vacancy, bool]:
    """Сохранение вакансии с дедупликацией по (user_id, hh_vacancy_id).

    Args:
        fields:        контент-поля парсинга (None не затирают сохранённое);
        ingest_status: 'raw' (парсинг прошёл) или 'error' (нет на hh, docs/04 §5).

    Returns:
        (вакансия, created): created=False — запись обновлена или пропущена
        (status='applied').

    Гонка двух одинаковых запросов гасится уникальным индексом
    uq_vacancies_user_hh_vacancy: IntegrityError → повторный SELECT.
    """
    content = {key: value for key, value in fields.items() if value is not None}

    for _attempt in range(2):
        existing = await db.scalar(
            select(Vacancy).where(
                Vacancy.user_id == user_id,
                Vacancy.hh_vacancy_id == hh_vacancy_id,
            )
        )

        if existing is None:
            vacancy = Vacancy(
                user_id=user_id,
                hh_vacancy_id=hh_vacancy_id,
                url=url,
                source=source,
                status=ingest_status,
                **content,
            )
            db.add(vacancy)
            try:
                await db.commit()
            except IntegrityError:
                # Параллельное создание той же вакансии → читаем чужую запись.
                await db.rollback()
                continue
            await db.refresh(vacancy)
            return vacancy, True

        # docs/04 §8: «Статус applied не перезаписывается автоматически».
        if existing.status == "applied":
            return existing, False

        # Обновление контент-полей (TASK: обновляем всё, кроме applied).
        for key, value in content.items():
            if key in _CONTENT_FIELDS:
                setattr(existing, key, value)
        existing.url = url
        existing.source = source

        if ingest_status == "error":
            existing.status = "error"  # вакансия удалена на hh (docs/04 §5)
        elif existing.status == "error":
            existing.status = "raw"  # успешный повторный парсинг → восстановление
        # analyzed / letter_ready не откатываем в raw — прогресс сохраняется.

        await db.commit()
        await db.refresh(existing)
        return existing, False

    raise AppError(
        409, "Не удалось сохранить вакансию: конфликт дубликатов", "DUPLICATE_VACANCY"
    )


async def list_user_vacancies(
    db: AsyncSession,
    user_id: uuid.UUID,
    *,
    status: str | None = None,
    source: str | None = None,
    search: str | None = None,
    min_match_score: int | None = None,
    page: int = 1,
    size: int = 20,
) -> tuple[Sequence[Vacancy], int]:
    """Список вакансий пользователя: фильтры + пагинация (docs/03 §4).

    Returns:
        (страница элементов, total) — total считается по тем же фильтрам.
    """
    conditions = [Vacancy.user_id == user_id]
    if status is not None:
        conditions.append(Vacancy.status == status)
    if source is not None:
        conditions.append(Vacancy.source == source)
    if search is not None and search.strip():
        pattern = f"%{search.strip()}%"
        conditions.append(
            or_(
                func.coalesce(Vacancy.title, "").ilike(pattern),
                func.coalesce(Vacancy.company_name, "").ilike(pattern),
            )
        )
    if min_match_score is not None:
        conditions.append(Vacancy.match_score >= min_match_score)

    total = await db.scalar(
        select(func.count()).select_from(Vacancy).where(*conditions)
    )
    rows = await db.scalars(
        select(Vacancy)
        .where(*conditions)
        .order_by(Vacancy.created_at.desc(), Vacancy.id)
        .offset((page - 1) * size)
        .limit(size)
    )
    return list(rows.all()), int(total or 0)


def change_status(vacancy: Vacancy, new_status: str) -> bool:
    """Переход статуса по ALLOWED_TRANSITIONS; нарушение → 409.

    Returns:
        True — статус изменён, False — уже целевой (идемпотентный PATCH).
    """
    if vacancy.status == new_status:
        return False
    allowed = ALLOWED_TRANSITIONS.get(vacancy.status, frozenset())
    if new_status not in allowed:
        targets = ", ".join(sorted(allowed)) or "нет (терминальный статус)"
        raise AppError(
            409,
            f"Недопустимый переход статуса «{vacancy.status}» → «{new_status}»; "
            f"разрешены: {targets}",
            "INVALID_STATUS_TRANSITION",
        )
    vacancy.status = new_status
    return True


async def delete_vacancy(db: AsyncSession, vacancy: Vacancy) -> None:
    """Физическое удаление вакансии (мягкого удаления нет — docs/02 §1)."""
    await db.delete(vacancy)
    await db.commit()
