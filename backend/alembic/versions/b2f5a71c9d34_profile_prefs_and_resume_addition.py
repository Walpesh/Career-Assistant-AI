"""add profile analysis preferences and resume addition

Revision ID: b2f5a71c9d34
Revises: 67b6d9942cfc
Create Date: 2026-10-02 10:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'b2f5a71c9d34'
down_revision: Union[str, None] = '67b6d9942cfc'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # analysis_preferences — пожелания пользователя на человеческом языке
    #   («не хочу трудоустройство по ТК РФ»), напрямую влияют на этап анализа
    #   (docs/05_LLM_PIPELINE.md §4).
    # resume_addition — текст из поля «Хотите добавить информацию в конец
    #   резюме?», дописывается в конец письма скриптовым методом (docs/05 §5, §6).
    op.add_column(
        'user_profiles',
        sa.Column('analysis_preferences', sa.Text(), nullable=True),
    )
    op.add_column(
        'user_profiles',
        sa.Column('resume_addition', sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('user_profiles', 'resume_addition')
    op.drop_column('user_profiles', 'analysis_preferences')