"""add progress_message and progress_stage to tasks

Revision ID: a7b8c9d0e1f2
Revises: f1a2b3c4d5e6
Create Date: 2026-10-03 14:10:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'a7b8c9d0e1f2'
down_revision: Union[str, None] = 'f1a2b3c4d5e6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Прогресс задачи сохраняется в БД на каждом шаге (docs/04 §6), чтобы после
    # перезагрузки страницы GET /tasks отдавал текущий этап/сообщение.
    op.add_column('tasks', sa.Column('progress_message', sa.Text(), nullable=True))
    op.add_column('tasks', sa.Column('progress_stage', sa.String(length=32), nullable=True))


def downgrade() -> None:
    op.drop_column('tasks', 'progress_stage')
    op.drop_column('tasks', 'progress_message')
