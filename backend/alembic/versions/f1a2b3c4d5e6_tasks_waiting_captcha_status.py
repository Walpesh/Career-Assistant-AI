"""add waiting_captcha to tasks.status CHECK constraint

Revision ID: f1a2b3c4d5e6
Revises: c3a9f0d1e4b7
Create Date: 2026-10-03 14:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'f1a2b3c4d5e6'
down_revision: Union[str, None] = 'c3a9f0d1e4b7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # docs/04 §2 п.3, §5: капча переводит задачу в waiting_captcha (пауза под
    # ручное вмешательство), а не в failed — состояние должно проходить CHECK.
    op.drop_constraint('ck_tasks_status', 'tasks', type_='check')
    op.create_check_constraint(
        'ck_tasks_status',
        'tasks',
        "status IN ('pending', 'processing', 'completed', 'failed', 'waiting_captcha')",
    )


def downgrade() -> None:
    # Задачи, «застрявшие» в waiting_captcha, возвращаются в failed.
    op.execute("UPDATE tasks SET status = 'failed' WHERE status = 'waiting_captcha'")
    op.drop_constraint('ck_tasks_status', 'tasks', type_='check')
    op.create_check_constraint(
        'ck_tasks_status',
        'tasks',
        "status IN ('pending', 'processing', 'completed', 'failed')",
    )
