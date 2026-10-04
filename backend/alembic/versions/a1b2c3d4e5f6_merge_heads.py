"""merge two heads: tasks progress stage + billing privacy usage

Revision ID: a1b2c3d4e5f6
Revises: a7b8c9d0e1f2, d4e5f6a7b8c9
Create Date: 2026-10-04 12:00:00.000000

Ветки a7b8c9d0e1f2 (tasks.progress) и d4e5f6a7b8c9 (billing/privacy)
разошлись от общего предка f1a2b3c4d5e6 — у схемы было два head'а и
``alembic upgrade head`` падал с MultipleHeadsError. Точка слияния
фиксирует обе ветки; содержательные изменения следующей ревизией.
"""

from typing import Sequence, Union

# revision identifiers, used by Alembic.
revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, Sequence[str], None] = ("a7b8c9d0e1f2", "d4e5f6a7b8c9")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Merge-точка: обе ветки уже применены, DDL не требуется."""


def downgrade() -> None:
    """Downgrade через multiple heads не поддерживается alembic по умолчанию."""
    pass
