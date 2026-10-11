"""Мульти-источниковая архитектура парсинга (docs/04_PARSING_RULES.md §10).

Пакет содержит контракт адаптера источника, реестр, адаптер hh.ru, адаптер
RemoteOK, адаптер Remotive, адаптер We Work Remotely, адаптер Greenhouse,
адаптер Lever, адаптер Ashby и адаптер Trudvsem. Импорт пакета не тянет heavy-зависимости:
``HHAdapter``, ``RemoteOKAdapter``, ``RemotiveAdapter``,
``WeWorkRemotelyAdapter``, ``GreenhouseAdapter``, ``LeverAdapter``,
``AshbyAdapter`` и ``TrudvsemAdapter`` подключаются лениво через ``default_registry()``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.modules.parsing.sources import registry as _registry_module
from app.modules.parsing.sources.base import BaseSourceAdapter
from app.modules.parsing.sources.registry import SourceRegistry, UnknownSourceError

if TYPE_CHECKING:
    from app.modules.parsing.sources.ashby import AshbyAdapter
    from app.modules.parsing.sources.greenhouse import GreenhouseAdapter
    from app.modules.parsing.sources.lever import LeverAdapter
    from app.modules.parsing.sources.remoteok import RemoteOKAdapter
    from app.modules.parsing.sources.remotive import RemotiveAdapter
    from app.modules.parsing.sources.trudvsem import TrudvsemAdapter
    from app.modules.parsing.sources.weworkremotely import WeWorkRemotelyAdapter

__all__ = [
    "AshbyAdapter",
    "BaseSourceAdapter",
    "SourceRegistry",
    "UnknownSourceError",
    "default_registry",
    "GreenhouseAdapter",
    "LeverAdapter",
    "RemoteOKAdapter",
    "RemotiveAdapter",
    "TrudvsemAdapter",
    "WeWorkRemotelyAdapter",
]


def default_registry() -> SourceRegistry:
    """Реестр со встроенными источниками: hh, remoteok, remotive, weworkremotely, greenhouse, lever, ashby, trudvsem.

    Базовый ``registry.default_registry()`` даёт встроенный ``HHAdapter``;
    сюда дополнительно регистрируются ``RemoteOKAdapter``,
    ``RemotiveAdapter``, ``WeWorkRemotelyAdapter``, ``GreenhouseAdapter``,
    ``LeverAdapter``, ``AshbyAdapter`` и ``TrudvsemAdapter``.
    Оркестратор получает реестр именно через пакет
    ``app.modules.parsing.sources``, поэтому подключение источника не требует
    правок оркестратора (docs/04 §10.3).
    """
    registry = _registry_module.default_registry()
    from app.modules.parsing.sources.ashby import AshbyAdapter
    from app.modules.parsing.sources.greenhouse import GreenhouseAdapter
    from app.modules.parsing.sources.lever import LeverAdapter
    from app.modules.parsing.sources.remoteok import RemoteOKAdapter
    from app.modules.parsing.sources.remotive import RemotiveAdapter
    from app.modules.parsing.sources.trudvsem import TrudvsemAdapter
    from app.modules.parsing.sources.weworkremotely import WeWorkRemotelyAdapter

    registry.register(RemoteOKAdapter)
    registry.register(RemotiveAdapter)
    registry.register(WeWorkRemotelyAdapter)
    registry.register(GreenhouseAdapter)
    registry.register(LeverAdapter)
    registry.register(AshbyAdapter)
    registry.register(TrudvsemAdapter)
    return registry


def __getattr__(name: str) -> Any:
    """Ленивый доступ к адаптерам источников без импорта при загрузке пакета."""
    if name == "AshbyAdapter":
        from app.modules.parsing.sources.ashby import AshbyAdapter

        return AshbyAdapter
    if name == "GreenhouseAdapter":
        from app.modules.parsing.sources.greenhouse import GreenhouseAdapter

        return GreenhouseAdapter
    if name == "LeverAdapter":
        from app.modules.parsing.sources.lever import LeverAdapter

        return LeverAdapter
    if name == "RemoteOKAdapter":
        from app.modules.parsing.sources.remoteok import RemoteOKAdapter

        return RemoteOKAdapter
    if name == "RemotiveAdapter":
        from app.modules.parsing.sources.remotive import RemotiveAdapter

        return RemotiveAdapter
    if name == "TrudvsemAdapter":
        from app.modules.parsing.sources.trudvsem import TrudvsemAdapter

        return TrudvsemAdapter
    if name == "WeWorkRemotelyAdapter":
        from app.modules.parsing.sources.weworkremotely import WeWorkRemotelyAdapter

        return WeWorkRemotelyAdapter
    raise AttributeError(f"модуль {__name__!r} не содержит атрибута {name!r}")
