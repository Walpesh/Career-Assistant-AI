"""Компактный Prometheus-реестр (fallback без ``prometheus_client``).

Реализует минимальный интерфейс ``Counter``/``Gauge``/``Histogram`` и
text-exposition формата Prometheus 0.0.4. Используется только когда пакет
``prometheus_client`` не установлен — в production/CI он присутствует,
fallback гарантирует работоспособность ``GET /metrics`` в любом случае.
"""

from __future__ import annotations

import threading
from typing import Any

__all__ = ["Counter", "Gauge", "Histogram", "SimpleRegistry"]


def _escape(value: str) -> str:
    """Экранировать значение label'а согласно формату Prometheus."""
    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _format_labels(names: tuple[str, ...], values: tuple[str, ...]) -> str:
    if not names:
        return ""
    pairs = ",".join(f'{n}="{_escape(v)}"' for n, v in zip(names, values, strict=True))
    return "{" + pairs + "}"


def _labels_plus(base: str, name: str, value: str) -> str:
    """Добавить дополнительный label к уже построенному набору."""
    escaped = f'{name}="{_escape(value)}"'
    if not base:
        return "{" + escaped + "}"
    return base[:-1] + "," + escaped + "}"


def _label_key(kwargs: dict[str, Any]) -> tuple[tuple[str, str], ...]:
    return tuple(sorted((str(k), str(v)) for k, v in kwargs.items()))


class _Metric:
    """Общая база: потокобезопасное хранение значений по набору label'ов."""

    def __init__(
        self,
        name: str,
        documentation: str,
        labelnames: tuple[str, ...] = (),
        registry: Any = None,
        **_extra: Any,
    ) -> None:
        self._name = name
        self._documentation = documentation
        self._labelnames = tuple(labelnames)
        self._values: dict[tuple[tuple[str, str], ...], float] = {}
        self._lock = threading.Lock()
        if registry is not None:
            registry.register(self)

    def labels(self, *args: Any, **kwargs: Any) -> _Metric:
        return self

    def inc(self, amount: float = 1.0, **kwargs: Any) -> None:
        key = _label_key(kwargs)
        with self._lock:
            self._values[key] = self._values.get(key, 0.0) + float(amount)

    def dec(self, amount: float = 1.0, **kwargs: Any) -> None:
        self.inc(-amount, **kwargs)

    def set(self, value: float, **kwargs: Any) -> None:
        key = _label_key(kwargs)
        with self._lock:
            self._values[key] = float(value)

    def observe(self, value: float, **kwargs: Any) -> None:
        key = _label_key(kwargs)
        with self._lock:
            self._values[key] = self._values.get(key, 0.0) + float(value)

    def collect(self) -> list[str]:
        with self._lock:
            items = sorted(self._values.items())
        return [
            f"{self._name}{_format_labels(self._labelnames, tuple(v for _, v in key))} {value}"
            for key, value in items
        ]


class Counter(_Metric):
    """Монотонно растущий счётчик (экспортируется с суффиксом _total)."""

    def collect(self) -> list[str]:
        with self._lock:
            items = sorted(self._values.items())
        return [
            f"{self._name}_total"
            f"{_format_labels(self._labelnames, tuple(v for _, v in key))} {value}"
            for key, value in items
        ]


class Gauge(_Metric):
    """Значение, которое может как расти, так и падать."""


class Histogram(_Metric):
    """Гистограмма с buckets, ``_sum`` и ``_count``."""

    def __init__(
        self,
        name: str,
        documentation: str,
        labelnames: tuple[str, ...] = (),
        buckets: tuple[float, ...] = (),
        registry: Any = None,
        **_extra: Any,
    ) -> None:
        super().__init__(name, documentation, labelnames, registry=registry)
        self._buckets = tuple(sorted(buckets))
        self._counts: dict[tuple[tuple[str, str], ...], list[int]] = {}
        self._counts_lock = threading.Lock()

    def observe(self, value: float, **kwargs: Any) -> None:
        key = _label_key(kwargs)
        value = float(value)
        with self._counts_lock:
            counts = self._counts.setdefault(key, [0] * len(self._buckets))
            for index, bound in enumerate(self._buckets):
                if value <= bound:
                    counts[index] += 1
        self.inc(value, **kwargs)

    def collect(self) -> list[str]:
        lines: list[str] = []
        with self._counts_lock:
            snapshot = {key: list(counts) for key, counts in self._counts.items()}
        with self._lock:
            totals = dict(self._values)
        for key, counts in sorted(snapshot.items()):
            base = _format_labels(self._labelnames, tuple(v for _, v in key))
            for bound, count in zip(self._buckets, counts, strict=True):
                lines.append(f"{self._name}_bucket{_labels_plus(base, 'le', str(bound))} {count}")
            total = totals.get(key, 0.0)
            lines.append(f"{self._name}_bucket{_labels_plus(base, 'le', '+Inf')} {sum(counts)}")
            lines.append(f"{self._name}_sum{base} {total}")
            lines.append(f"{self._name}_count{base} {sum(counts)}")
        return lines


class SimpleRegistry:
    """Минимальный реестр: собирает exposition-текст со всех метрик."""

    def __init__(self, auto_describe: bool = False) -> None:
        self._metrics: list[_Metric] = []
        self.auto_describe = auto_describe

    def register(self, metric: _Metric) -> None:
        self._metrics.append(metric)

    def collect(self) -> list[str]:
        lines: list[str] = []
        for metric in self._metrics:
            if self.auto_describe:
                kind = (
                    "counter"
                    if isinstance(metric, Counter)
                    else "gauge"
                    if isinstance(metric, Gauge)
                    else "histogram"
                )
                lines.append(f"# HELP {metric._name} {metric._documentation}")
                lines.append(f"# TYPE {metric._name} {kind}")
            lines.extend(metric.collect())
        return lines

    def generate_latest(self) -> bytes:
        """Prometheus text exposition (версия 0.0.4)."""
        return ("\n".join(self.collect()) + "\n").encode("utf-8")
