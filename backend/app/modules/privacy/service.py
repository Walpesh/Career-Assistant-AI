"""Privacy Module — выгрузка и удаление персональных данных.

TASK «Privacy Compliance & Data Management»: право на доступ и право на
забвение по 152-ФЗ (ст. 14 «доступ», ст. 21 «удаление или уничтожение»)
и GDPR (ст. 15/17 — доступ и стирание).

Два эндпоинта (docs/03_API_CONTRACTS.md §10):

* ``GET  /api/v1/account/export`` — полная выгрузка персональных данных
  пользователя в JSON;
* ``DELETE /api/v1/account``      — каскадное удаление аккаунта.

Удаление **каскадное** не только на уровне ORM (``cascade="all,
delete-orphan"`` + ``ON DELETE CASCADE``), но и явно, поимённо, по каждой
таблице. Явный список нужен по двум причинам:

1. ``analyses`` / ``cover_letters`` связаны с вакансией, а не с
   пользователем напрямую — без поимённого удаления их пришлось бы
   доставать через ``vacancies.id``;
2. отчёт об удалении должен содержать **фактические** количества строк
   (152-ФЗ ст. 21 — подтверждение исполнения запроса).

Мягкого удаления в проекте нет (docs/02 §1): строки удаляются физически.
Пароль-хэш и хэши refresh-токенов в выгрузку **не попадают** — это
не персональные данные пользователя, а технические секреты; отдавать их
в ответе 200 означало бы упростить их компрометацию.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.models import (
    Analysis,
    CoverLetter,
    PaymentEvent,
    ProxyUsageLog,
    RefreshToken,
    Subscription,
    Task,
    UsageCounter,
    User,
    UserProfile,
    Vacancy,
)

__all__ = [
    "EXPORT_FORMAT_VERSION",
    "build_export",
    "count_user_rows",
    "delete_account_data",
]

log = get_logger(__name__)

#: Версия формата выгрузки: клиентам нужно знать, что схема меняется.
EXPORT_FORMAT_VERSION = "1.0"


def _iso(value: Any) -> Any:
    """datetime → ISO-8601; UUID/Decimal → строки (JSON-совместимость)."""
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    if isinstance(value, (uuid.UUID, Decimal)):
        return str(value)
    return value


def _row(model_object, fields: tuple[str, ...]) -> dict[str, Any]:
    """Словарь из указанных полей ORM-объекта (значения — в JSON-типы)."""
    return {name: _iso(getattr(model_object, name, None)) for name in fields}


#: Полей, которые **никогда** не попадают в выгрузку.
#: `password_hash` — не персональные данные, а секрет; `hashed_token` —
#: материал аутентификации, его выдача сделала бы экспорт инструментом
#: компрометации сессий.
_USER_FIELDS = ("id", "email", "is_active", "created_at", "updated_at")
_PROFILE_FIELDS = (
    "user_id",
    "full_name",
    "resume_text",
    "compact_resume",
    "skills",
    "experience_years",
    "desired_salary_from",
    "desired_salary_to",
    "match_threshold",
    "preferred_work_formats",
    "analysis_preferences",
    "resume_addition",
    "created_at",
    "updated_at",
)
_VACANCY_FIELDS = (
    "id",
    "hh_vacancy_id",
    "url",
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
    "description_raw",
    "description_html",
    "source",
    "status",
    "match_score",
    "published_at",
    "created_at",
    "updated_at",
)
_ANALYSIS_FIELDS = (
    "id",
    "vacancy_id",
    "match_score",
    "match_details",
    "strengths",
    "weaknesses",
    "summary",
    "created_at",
    "updated_at",
)
_LETTER_FIELDS = ("id", "vacancy_id", "content", "version", "created_at", "updated_at")
_TASK_FIELDS = (
    "id",
    "task_type",
    "status",
    "progress_current",
    "progress_total",
    "progress_stage",
    "progress_message",
    "payload",
    "result",
    "error_message",
    "related_vacancy_id",
    "created_at",
    "started_at",
    "finished_at",
)
#: Подписка и расход квот — тоже персональные данные (связаны с
#: идентификатором пользователя), поэтому выгружаются, но без
#: `external_id` платёжного шлюза: это идентификатор платежа, а не данные
#: кандидата, и в выгрузке он не нужен.
_SUBSCRIPTION_FIELDS = (
    "tier",
    "status",
    "provider",
    "daily_parsing_jobs",
    "daily_cover_letters",
    "daily_analyses",
    "daily_proxy_mb",
    "current_period_end",
    "canceled_at",
    "created_at",
    "updated_at",
)
_USAGE_FIELDS = ("day", "quota_kind", "used", "created_at", "updated_at")
_PROXY_USAGE_FIELDS = (
    "task_id",
    "bytes_total",
    "requests_total",
    "captcha_total",
    "created_at",
)


async def _rows(db: AsyncSession, model, condition, fields) -> list[dict[str, Any]]:
    """Список словарей по выборке модели (значения приведены к JSON-типам)."""
    result = await db.execute(select(model).where(condition))
    return [_row(item, fields) for item in result.scalars().all()]


async def build_export(db: AsyncSession, user: User) -> dict[str, Any]:
    """Собрать полный пакет персональных данных пользователя (JSON).

    Возвращает «сырые» данные всех таблиц, связанных с пользователем, плюс
    метаданные выгрузки. Состав соответствует 152-ФЗ ст. 14 (доступ) и
    GDPR ст. 15/20: получатель видит всё, что сервис о нём хранит.

    Пагинации здесь намеренно нет: цель выгрузки — полнота, а не скорость
    ответа, а усечение «первыми 20 строками» нарушало бы право на доступ.
    """
    vacancy_ids = list(
        (await db.scalars(select(Vacancy.id).where(Vacancy.user_id == user.id))).all()
    )

    # Анализы и письма связаны с вакансией, а не с пользователем напрямую,
    # поэтому фильтруются по списку его вакансий. Пустой список → пустые
    # выборки (условие id IS NULL исключает чужие строки).
    if vacancy_ids:
        analyses = await _rows(db, Analysis, Analysis.vacancy_id.in_(vacancy_ids), _ANALYSIS_FIELDS)
        letters = await _rows(
            db, CoverLetter, CoverLetter.vacancy_id.in_(vacancy_ids), _LETTER_FIELDS
        )
    else:
        analyses = []
        letters = []

    profile = await db.get(UserProfile, user.id)
    subscription = await db.scalar(select(Subscription).where(Subscription.user_id == user.id))
    usage_rows = list(
        (
            await db.scalars(
                select(UsageCounter)
                .where(UsageCounter.user_id == user.id)
                .order_by(UsageCounter.day.desc(), UsageCounter.quota_kind)
            )
        ).all()
    )
    proxy_rows = list(
        (
            await db.scalars(
                select(ProxyUsageLog)
                .where(ProxyUsageLog.user_id == user.id)
                .order_by(ProxyUsageLog.created_at.desc())
            )
        ).all()
    )

    return {
        "format_version": EXPORT_FORMAT_VERSION,
        "generated_at": datetime.now(UTC).isoformat(),
        "user": _row(user, _USER_FIELDS),
        "profile": _row(profile, _PROFILE_FIELDS) if profile is not None else None,
        "subscription": (
            _row(subscription, _SUBSCRIPTION_FIELDS) if subscription is not None else None
        ),
        "vacancies": await _rows(db, Vacancy, Vacancy.user_id == user.id, _VACANCY_FIELDS),
        "analyses": analyses,
        "cover_letters": letters,
        "tasks": await _rows(db, Task, Task.user_id == user.id, _TASK_FIELDS),
        "usage_counters": [_row(item, _USAGE_FIELDS) for item in usage_rows],
        "proxy_usage_logs": [_row(item, _PROXY_USAGE_FIELDS) for item in proxy_rows],
        "excluded_by_design": [
            "password_hash (секрет аутентификации, не персональные данные)",
            "refresh_tokens.hashed_token (материал сессии; удаляется с аккаунтом)",
        ],
    }


async def delete_account_data(db: AsyncSession, user: User) -> dict[str, int]:
    """Каскадно удалить все данные пользователя и вернуть отчёт по таблицам.

    Порядок важен: сначала строки, висящие на `vacancies` (анализы, письма),
    затем сами вакансии, и только потом профиль и аккаунт. Так количество
    удалённых строк всегда достоверно, а отчёт можно предъявить как
    подтверждение исполнения запроса (152-ФЗ ст. 21).

    Returns:
        Словарь {имя таблицы: количество удалённых строк}. ``users`` = 1 —
        это и есть факт удаления аккаунта.
    """
    report: dict[str, int] = {}

    vacancy_ids = list(
        (await db.scalars(select(Vacancy.id).where(Vacancy.user_id == user.id))).all()
    )

    async def _purge(model, condition) -> int:
        result = await db.execute(delete(model).where(condition))
        return int(result.rowcount or 0)

    # 1) Дети вакансий: анализы и письма связаны с ними, а не с user_id.
    if vacancy_ids:
        report["analyses"] = await _purge(Analysis, Analysis.vacancy_id.in_(vacancy_ids))
        report["cover_letters"] = await _purge(CoverLetter, CoverLetter.vacancy_id.in_(vacancy_ids))
    else:
        report["analyses"] = 0
        report["cover_letters"] = 0

    # 2) Логи прокси-трафика висят на tasks (ON DELETE CASCADE), но считаем
    #    их явно, чтобы в отчёте не пропала ни одна таблица с перс. данными.
    report["proxy_usage_logs"] = await _purge(ProxyUsageLog, ProxyUsageLog.user_id == user.id)
    report["tasks"] = await _purge(Task, Task.user_id == user.id)
    report["vacancies"] = await _purge(Vacancy, Vacancy.user_id == user.id)
    report["usage_counters"] = await _purge(UsageCounter, UsageCounter.user_id == user.id)
    report["subscriptions"] = await _purge(Subscription, Subscription.user_id == user.id)
    report["refresh_tokens"] = await _purge(RefreshToken, RefreshToken.user_id == user.id)

    # 3) Профиль (users 1─1 user_profiles, docs/02 §4).
    report["user_profiles"] = await _purge(UserProfile, UserProfile.user_id == user.id)

    # 4) Платёжные события привязаны к провайдеру, а не к user_id (в них нет
    #    ПД пользователя — только идентификатор платежа), поэтому их учёт
    #    обезличен: обнуляем ссылку вместо удаления. Иначе повторная
    #    доставка вебхука после удаления аккаунта воскресила бы подписку.
    await db.execute(
        PaymentEvent.__table__.update().where(PaymentEvent.user_id == user.id).values(user_id=None)
    )
    report["payment_events_anonymized"] = 1

    # 5) Аккаунт последним: после него каскад в БД подчистит всё, что не
    #    учтено явно (ON DELETE CASCADE объявлен на новых таблицах).
    await db.delete(user)
    report["users"] = 1

    await db.commit()

    # Итог без служебных счётчиков служебных операций.
    deleted_tables = sum(
        value for key, value in report.items() if key != "payment_events_anonymized"
    )
    log.info(
        "privacy: аккаунт удалён каскадно",
        tables_deleted=len(report) - 1,
        rows_deleted=deleted_tables,
    )
    report["total_rows_deleted"] = deleted_tables
    return report


async def count_user_rows(db: AsyncSession, user_id: uuid.UUID) -> dict[str, int]:
    """Количество строк пользователя по ключевым таблицам (сводка аккаунта)."""

    async def _count(model) -> int:
        return int(
            await db.scalar(select(func.count()).select_from(model).where(model.user_id == user_id))
            or 0
        )

    return {
        "vacancies": await _count(Vacancy),
        "tasks": await _count(Task),
        "refresh_tokens": await _count(RefreshToken),
        "usage_counters": await _count(UsageCounter),
    }
