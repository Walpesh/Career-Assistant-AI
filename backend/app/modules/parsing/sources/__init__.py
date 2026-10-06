"""Мульти-источниковая архитектура парсинга (docs/04_PARSING_RULES.md §10).

Пакет содержит контракт адаптера источника, реестр и адаптер hh.ru.
Импорт пакета не тянет heavy-зависимости: ``HHAdapter`` подключается
лениво через ``default_registry()``.
"""

from app.modules.parsing.sources.base import BaseSourceAdapter
from app.modules.parsing.sources.registry import (
    SourceRegistry,
    UnknownSourceError,
    default_registry,
)

__all__ = [
    "BaseSourceAdapter",
    "SourceRegistry",
    "UnknownSourceError",
    "default_registry",
]
