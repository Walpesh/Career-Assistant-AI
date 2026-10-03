"""add subscriptions, usage_counters, payment_events, proxy_usage_logs

Revision ID: d4e5f6a7b8c9
Revises: f1a2b3c4d5e6
Create Date: 2026-10-04 10:00:00.000000

TASK «Legal Framework / Monetization / Proxy Cost Tracking»
(docs/02_DATABASE.md §3.8–§3.11):

* ``subscriptions``   — тарифный план пользователя (free / pro / enterprise);
* ``usage_counters``  — расход суточных квот (parse/letter/analysis/proxy_mb);
* ``payment_events``  — ключ идемпотентности вебхуков платёжных шлюзов;
* ``proxy_usage_logs`` — объём прокси-трафика и доля капчи по задачам.

У всех таблиц внешний ключ на ``users.id`` объявлен с ``ON DELETE CASCADE``:
это вторая половина каскадного удаления в ``DELETE /api/v1/account``
(первая — явные DELETE поимённо в Privacy Module, docs/03 §10). Каскад в БД
нужен и для аварийных случаев: если удалить пользователя минуя API, «хвосты»
персональных данных не должны остаться (152-ФЗ ст. 21).
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "d4e5f6a7b8c9"
down_revision: Union[str, None] = "f1a2b3c4d5e6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- subscriptions (docs/02 §3.8) -----------------------------------------
    op.create_table(
        "subscriptions",
        sa.Column(
            "id",
            sa.UUID(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("tier", sa.String(length=16), server_default=sa.text("'free'"), nullable=False),
        sa.Column(
            "status", sa.String(length=16), server_default=sa.text("'active'"), nullable=False
        ),
        sa.Column("provider", sa.String(length=32), nullable=True),
        sa.Column("external_id", sa.String(length=128), nullable=True),
        sa.Column("daily_parsing_jobs", sa.Integer(), nullable=True),
        sa.Column("daily_cover_letters", sa.Integer(), nullable=True),
        sa.Column("daily_analyses", sa.Integer(), nullable=True),
        sa.Column("daily_proxy_mb", sa.Integer(), nullable=True),
        sa.Column("current_period_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("canceled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.CheckConstraint(
            "tier IN ('free', 'pro', 'enterprise')", name="ck_subscriptions_tier"
        ),
        sa.CheckConstraint(
            "status IN ('active', 'past_due', 'canceled', 'expired')",
            name="ck_subscriptions_status",
        ),
        sa.CheckConstraint(
            "daily_parsing_jobs >= 0 AND daily_cover_letters >= 0 "
            "AND daily_analyses >= 0 AND daily_proxy_mb >= 0",
            name="ck_subscriptions_quotas_non_negative",
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        # Один тариф на пользователя: строка активной подписки единственна.
        sa.UniqueConstraint("user_id", name="uq_subscriptions_user_id"),
    )

    # --- usage_counters (docs/02 §3.9) ----------------------------------------
    op.create_table(
        "usage_counters",
        sa.Column(
            "id",
            sa.UUID(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("quota_kind", sa.String(length=16), nullable=False),
        sa.Column("used", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.CheckConstraint(
            "quota_kind IN ('parse', 'letter', 'analysis', 'proxy_mb')",
            name="ck_usage_counters_kind",
        ),
        sa.CheckConstraint("used >= 0", name="ck_usage_counters_used_non_negative"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    # Уникальный индекс — одновременно путь ON CONFLICT для атомарного
    # начисления квоты (Billing Module _increment).
    op.create_index(
        "ux_usage_counters_user_day_kind",
        "usage_counters",
        ["user_id", "day", "quota_kind"],
        unique=True,
    )
    op.create_index("ix_usage_counters_day", "usage_counters", ["day"])

    # --- payment_events (docs/02 §3.10) ---------------------------------------
    # Ключ идемпотентности (provider, external_event_id): повторная доставка
    # вебхука не должна повторно менять тариф. user_id — nullable и без
    # FK: после удаления аккаунта запись обезличивается, но сам факт платежа
    # и его идентификатор у шлюза остаются (см. Privacy Module).
    op.create_table(
        "payment_events",
        sa.Column(
            "id",
            sa.UUID(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("external_event_id", sa.String(length=128), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=True),
        sa.Column("tier", sa.String(length=16), nullable=True),
        sa.Column(
            "status", sa.String(length=16), server_default=sa.text("'processed'"), nullable=False
        ),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.CheckConstraint(
            "status IN ('processed', 'ignored', 'failed')", name="ck_payment_events_status"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ux_payment_events_provider_external",
        "payment_events",
        ["provider", "external_event_id"],
        unique=True,
    )
    op.create_index("ix_payment_events_user_id", "payment_events", ["user_id"])

    # --- proxy_usage_logs (docs/02 §3.11) -------------------------------------
    op.create_table(
        "proxy_usage_logs",
        sa.Column(
            "id",
            sa.UUID(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("task_id", sa.UUID(), nullable=False),
        sa.Column("bytes_total", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("requests_total", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("captcha_total", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.CheckConstraint("bytes_total >= 0", name="ck_proxy_usage_logs_bytes_non_negative"),
        sa.CheckConstraint(
            "requests_total >= 0", name="ck_proxy_usage_logs_requests_non_negative"
        ),
        sa.CheckConstraint(
            "captcha_total >= 0", name="ck_proxy_usage_logs_captcha_non_negative"
        ),
        # Капча не может быть чаще ответов — страховка от ошибки в счётчиках.
        sa.CheckConstraint(
            "captcha_total <= requests_total",
            name="ck_proxy_usage_logs_captcha_lte_requests",
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    # Одна строка на задачу: повторный flush после ретрая обновляет её,
    # а не создаёт дубль (иначе расход квоты удваивался бы).
    op.create_index("ux_proxy_usage_logs_task_id", "proxy_usage_logs", ["task_id"], unique=True)
    op.create_index("ix_proxy_usage_logs_user_id", "proxy_usage_logs", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_proxy_usage_logs_user_id", table_name="proxy_usage_logs")
    op.drop_index("ux_proxy_usage_logs_task_id", table_name="proxy_usage_logs")
    op.drop_table("proxy_usage_logs")
    op.drop_index("ix_payment_events_user_id", table_name="payment_events")
    op.drop_index("ux_payment_events_provider_external", table_name="payment_events")
    op.drop_table("payment_events")
    op.drop_index("ix_usage_counters_day", table_name="usage_counters")
    op.drop_index("ux_usage_counters_user_day_kind", table_name="usage_counters")
    op.drop_table("usage_counters")
    op.drop_table("subscriptions")