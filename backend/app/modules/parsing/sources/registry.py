"""Реестр адаптеров источников (docs/04_PARSING_RULES.md §10.3).

``SourceRegistry`` — единственная точка подключения источников для
ParsingOrchestrator: адаптеры регистрируются по ``source_name``, доставляются
по имени и исполняются через ``execute``. Новый источник подключается
регистрацией своего адаптера — без правок оркестратора и очереди.

Реестр хранит либо класс адаптера (тогда ``create`` инстанцирует его,
прокидывая, например, общую ``AntiBanSession``), либо готовый экземпляр
(удобно для тестов и ручной сборки).
"""

from __future__ import annotations

import inspect
from typing import Any

from app.modules.parsing.sources.base import BaseSourceAdapter

__all__ = [
    "UnknownSourceError",
    "SourceRegistry",
    "default_registry",
]


class UnknownSourceError(LookupError):
    """Источник не зарегистрирован в SourceRegistry."""

    def __init__(self, source_name: str, known: tuple[str, ...]) -> None:
        self.source_name = source_name
        self.known_sources = known
        known_list = ", ".join(known) or "нет"
        super().__init__(
            f"Источник «{source_name}» не зарегистрирован; доступные: {known_list}"
        )


class SourceRegistry:
    """Реестр адаптеров источников: register → get/create → execute."""

    def __init__(self) -> None:
        self._adapters: dict[str, BaseSourceAdapter | type[BaseSourceAdapter]] = {}

    # --- регистрация и доступ ------------------------------------------------

    def register(
        self, adapter: BaseSourceAdapter | type[BaseSourceAdapter]
    ) -> BaseSourceAdapter | type[BaseSourceAdapter]:
        """Зарегистрировать адаптер (класс или экземпляр) по его source_name.

        Returns:
            Переданный адаптер — можно строить цепочки ``registry.register(X)``.

        Raises:
            TypeError: у адаптера нет строкового source_name.
        """
        name = adapter.source_name
        if not isinstance(name, str) or not name.strip():
            raise TypeError(
                "Адаптер источника должен иметь непустой строковый source_name "
                f"(получено: {name!r})"
            )
        self._adapters[name] = adapter
        return adapter

    def get(self, source_name: str) -> BaseSourceAdapter | type[BaseSourceAdapter]:
        """Достать зарегистрированный адаптер; неизвестный источник → ошибка."""
        try:
            return self._adapters[source_name]
        except KeyError:
            raise UnknownSourceError(source_name, self.names()) from None

    def names(self) -> tuple[str, ...]:
        """Зарегистрированные источники в порядке регистрации."""
        return tuple(self._adapters)

    def __contains__(self, source_name: object) -> bool:
        return source_name in self._adapters

    def create(
        self, source_name: str, **kwargs: Any
    ) -> BaseSourceAdapter:
        """Экземпляр адаптера для источника.

        Класс инстанцируется с переданными kwargs (например, общей
        ``session``), готовый экземпляр возвращается как есть.
        """
        entry = self.get(source_name)
        if isinstance(entry, type):
            return entry(**kwargs)
        return entry

    async def execute(
        self, source_name: str, method: str, *args: Any, **kwargs: Any
    ) -> Any:
        """Исполнить метод адаптера по имени источника.

        Поддерживает синхронные и асинхронные методы: корутина awaited здесь.

        Raises:
            UnknownSourceError: источник не зарегистрирован.
            AttributeError: у адаптера нет такого метода.
        """
        adapter = self.create(source_name)
        target = getattr(adapter, method, None)
        if target is None or not callable(target):
            raise AttributeError(
                f"Адаптер источника «{source_name}» не имеет метода «{method}»"
            )
        result = target(*args, **kwargs)
        if inspect.isawaitable(result):
            result = await result
        return result


def default_registry() -> SourceRegistry:
    """Реестр со встроенным источником hh (docs/04 §10.3, по умолчанию)."""
    from app.modules.parsing.sources.hh import HHAdapter

    registry = SourceRegistry()
    registry.register(HHAdapter)
    return registry
