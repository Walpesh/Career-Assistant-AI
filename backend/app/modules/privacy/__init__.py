"""Privacy Module — выгрузка, сводка и каскадное удаление данных.

TASK «Privacy Compliance & Data Management»: 152-ФЗ ст. 14 (доступ) и
ст. 21 (удаление), GDPR ст. 15/17. Эндпоинты описаны в
docs/03_API_CONTRACTS.md §10, схема хранения — docs/02_DATABASE.md.
"""

from app.modules.privacy.router import router
from app.modules.privacy.service import (
    EXPORT_FORMAT_VERSION,
    build_export,
    count_user_rows,
    delete_account_data,
)

__all__ = [
    "EXPORT_FORMAT_VERSION",
    "build_export",
    "count_user_rows",
    "delete_account_data",
    "router",
]
