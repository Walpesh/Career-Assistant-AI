"""Мульти-источниковая архитектура парсинга (docs/04_PARSING_RULES.md §10).

Пакет содержит контракт адаптера источника, реестр, адаптер hh.ru и адаптер
RemoteOK. Импорт пакета не тянет heavy-зависимости: ``HHAdapter`` и
``RemoteOKAdapter`` подключаются лениво через ``default_registry()``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.modules.parsing.sources import registry as _registry_module
from app.modules.parsing.sources.base import BaseSourceAdapter
from app.modules.parsing.sources.registry import SourceRegistry, UnknownSourceError

if TYPE_CHECKING:
    from app.modules.parsing.sources.remoteok import RemoteOKAdapter

__all__ = [
    "BaseSourceAdapter",
    "SourceRegistry",
    "UnknownSourceError",
    "default_registry",
    "RemoteOKAdapter",
]


def default_registry() -> SourceRegistry:
    """Реестр со встроенными источниками: hh и remoteok (docs/04 §10.3).

    Базовый ``registry.default_registry()`` даёт встроенный ``HHAdapter``;
    сюда дополнительно регистрируется ``RemoteOKAdapter``. Оркестратор
    получает реестр именно через пакет ``app.modules.parsing.sources``,
    поэтому подключение источника не требует правок оркестратора.
    """
    registry = _registry_module.default_registry()
    from app.modules.parsing.sources.remoteok import RemoteOKAdapter

    registry.register(RemoteOKAdapter)
    return registry


def __getattr__(name: str) -> Any:
    """Ленивый доступ к ``RemoteOKAdapter`` без импорта при загрузке пакета."""
    if name == "RemoteOKAdapter":
        from app.modules.parsing.sources.remoteok import RemoteOKAdapter

        return RemoteOKAdapter
    raise AttributeError(f"модуль {__name__!r} не содержит атрибута {name!r}")

