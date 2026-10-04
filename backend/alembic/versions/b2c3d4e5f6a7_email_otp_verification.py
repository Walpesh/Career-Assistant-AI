"""add users.is_verified and email_otps (email OTP verification)

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-10-04 12:05:00.000000

TASK «Email OTP Code Verification System for User Registration»:

* ``users.is_verified`` — флаг подтверждения email (default false);
  до подтверждения POST /auth/login отвечает 403 EMAIL_NOT_VERIFIED;
* ``email_otps`` — активный 6-значный OTP-код на email: хранится только
  HMAC-SHA256-хэш (otp_code_hash), TTL 10 минут (expires_at), максимум
  5 попыток ввода (attempts_count). created_at — момент последней
  отправки, он же rate-limit resend (1 запрос / 60 сек на email).
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "b2c3d4e5f6a7"
down_revision: Union[str, Sequence[str], None] = "a1b2c3d4e5f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- users.is_verified (docs/02 §3.1) ------------------------------------
    op.add_column(
        "users",
        sa.Column("is_verified", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )

    # --- email_otps ----------------------------------------------------------
    op.create_table(
        "email_otps",
        sa.Column(
            "id",
            sa.UUID(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("otp_code_hash", sa.String(length=128), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempts_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.CheckConstraint("attempts_count >= 0", name="ck_email_otps_attempts_non_negative"),
        sa.PrimaryKeyConstraint("id"),
    )
    # Одна активная запись на email: повторная отправка перезаписывает код.
    op.create_index("ux_email_otps_email", "email_otps", ["email"], unique=True)


def downgrade() -> None:
    op.drop_index("ux_email_otps_email", table_name="email_otps")
    op.drop_table("email_otps")
    op.drop_column("users", "is_verified")
