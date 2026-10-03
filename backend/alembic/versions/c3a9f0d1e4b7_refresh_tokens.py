"""add refresh_tokens table (rotation + reuse detection)

Revision ID: c3a9f0d1e4b7
Revises: b2f5a71c9d34
Create Date: 2026-10-03 12:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'c3a9f0d1e4b7'
down_revision: Union[str, None] = 'b2f5a71c9d34'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # refresh_tokens — серверный реестр refresh-токенов: хранится только
    # SHA-256 хэш токена (hashed_token). revoked_at != NULL — токен отозван
    # (ротация или logout); повторное предъявление отозванного токена
    # трактуется как reuse и отзывает все токены пользователя.
    op.create_table(
        'refresh_tokens',
        sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
        sa.Column('user_id', sa.UUID(), nullable=False),
        sa.Column('jti', sa.String(length=64), nullable=False),
        sa.Column('hashed_token', sa.String(length=128), nullable=False),
        sa.Column('user_agent', sa.String(length=512), nullable=True),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_refresh_tokens_user_id', 'refresh_tokens', ['user_id'], unique=False)
    op.create_index('ix_refresh_tokens_jti', 'refresh_tokens', ['jti'], unique=True)
    op.create_index(
        'ix_refresh_tokens_hashed_token', 'refresh_tokens', ['hashed_token'], unique=True
    )


def downgrade() -> None:
    op.drop_index('ix_refresh_tokens_hashed_token', table_name='refresh_tokens')
    op.drop_index('ix_refresh_tokens_jti', table_name='refresh_tokens')
    op.drop_index('ix_refresh_tokens_user_id', table_name='refresh_tokens')
    op.drop_table('refresh_tokens')
