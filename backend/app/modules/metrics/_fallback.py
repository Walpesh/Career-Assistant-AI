"""Компактный Prometheus-реестр (fallback без ``prometheus_client``).

Реализует минимальный интерфейс ``Counter``/``Gauge``/``Histogram`` и
text-exposition формата Prometheus 0.0.4. Используется только когда пакет
``prometheus_client`` не установлен — в production/CI он присутствует,
fallback гарантирует работоспособность ``GET /metrics`` в любом случае.
"""

from __future__ import annotations

import threading
from typing import Any, NamedTuple

__all__ = ["Counter", "Gauge", "Histogram", "SimpleRegistry"]


class Sample(NamedTuple):
    """Совместимость с ``prometheus_client.sample.Sample`` (для collectors)."""

    name: str
    labels: dict[str, str]
    value: float
    timestamp: float | None = None



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
        self._values: dict[tuple[tuple[str, ...], ...], float] = {}
        self._lock = threading.Lock()
        if registry is not None:
            registry.register(self)

    def _resolve_labels(self, args: tuple[Any, ...], kwargs: dict[str, Any]) -> tuple[tuple[str, str], ...]:
        """Собрать полный набор label'ов из позиционных и именованных аргументов.

        Позиционные значения соответствуют ``labelnames`` по порядку (как в
        ``prometheus_client``), именованные — по имени. Результирующий ключ
        всегда содержит все объявленные label'и, иначе text-exposition падает
        в ``zip(strict=True)``.
        """
        bound: dict[str, str] = {}
        if args:
            if len(args) != len(self._labelnames):
                raise ValueError(
                    f"{self._name}: ожидалось {len(self._labelnames)} label'ов, получено {len(args)}"
                )
            bound.update(zip(self._labelnames, (str(v) for v in args), strict=True))
        bound.update({str(k): str(v) for k, v in kwargs.items()})
        missing = [name for name in self._labelnames if name not in bound]
        if missing:
            raise ValueError(f"{self._name}: не переданы label'и {missing}")
        extra = [name for name in bound if name not in self._labelnames]
        if extra:
            raise ValueError(f"{self._name}: необъявленные label'и {extra}")
        return tuple(sorted(bound.items()))

    def labels(self, *args: Any, **kwargs: Any) -> _BoundMetric:
        """Привязать значения label'ов и вернуть привязанную метрику."""
        return _BoundMetric(self, self._resolve_labels(args, kwargs))

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

    @property
    def samples(self) -> list[Sample]:
        """Samples в формате ``prometheus_client`` (для metrics_snapshot и др.)."""
        with self._lock:
            items = sorted(self._values.items())
        return [Sample(self._name, dict(key), value, None) for key, value in items]


class _BoundMetric:
    """Метрика с заранее привязанными значениями label'ов (результат ``labels()``)."""

    __slots__ = ("_metric", "_key")

    def __init__(self, metric: _Metric, key: tuple[tuple[str, str], ...]) -> None:
        self._metric = metric
        self._key = key

    def inc(self, amount: float = 1.0, **_kwargs: Any) -> None:
        self._metric.inc(amount, **dict(self._key))

    def dec(self, amount: float = 1.0, **_kwargs: Any) -> None:
        self._metric.dec(amount, **dict(self._key))

    def set(self, value: float, **_kwargs: Any) -> None:
        self._metric.set(value, **dict(self._key))

    def observe(self, value: float, **_kwargs: Any) -> None:
        self._metric.observe(value, **dict(self._key))



class Counter(_Metric):
    """Монотонно растущий счётчик (экспортируется с суффиксом _total)."""

    @property
    def _sample_name(self) -> str:
        """Имя sample без двойного ``_total`` (как в prometheus_client)."""
        return self._name if self._name.endswith("_total") else f"{self._name}_total"

    def collect(self) -> list[str]:
        with self._lock:
            items = sorted(self._values.items())
        return [
            f"{self._sample_name}"
            f"{_format_labels(self._labelnames, tuple(v for _, v in key))} {value}"
            for key, value in items
        ]

    @property
    def samples(self) -> list[Sample]:
        with self._lock:
            items = sorted(self._values.items())
        return [Sample(self._sample_name, dict(key), value, None) for key, value in items]


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

    @property
    def samples(self) -> list[Sample]:
        out: list[Sample] = []
        with self._counts_lock:
            snapshot = {key: list(counts) for key, counts in self._counts.items()}
        with self._lock:
            totals = dict(self._values)
        for key, counts in sorted(snapshot.items()):
            labels = dict(key)
            for bound, count in zip(self._buckets, counts, strict=True):
                out.append(
                    Sample(f"{self._name}_bucket", {**labels, "le": str(bound)}, float(count), None)
                )
            out.append(
                Sample(f"{self._name}_bucket", {**labels, "le": "+Inf"}, float(sum(counts)), None)
            )
            out.append(Sample(f"{self._name}_sum", labels, totals.get(key, 0.0), None))
            out.append(Sample(f"{self._name}_count", labels, float(sum(counts)), None))
        return out


class _MetricFamily:
    """Совместимость с ``prometheus_client.Metric`` (имя + samples)."""

    __slots__ = ("_metric",)

    def __init__(self, metric: _Metric) -> None:
        self._metric = metric

    @property
    def name(self) -> str:
        return self._metric._name

    @property
    def samples(self) -> list[Sample]:
        return self._metric.samples


class SimpleRegistry:
    """Минимальный реестр: собирает exposition-текст со всех метрик."""

    def __init__(self, auto_describe: bool = False) -> None:
        self._metrics: list[_Metric] = []
        self.auto_describe = auto_describe

    def register(self, metric: _Metric) -> None:
        self._metrics.append(metric)

    def collect(self) -> list[_MetricFamily]:
        """Metric-семействы в формате ``prometheus_client`` (для collectors)."""
        return [_MetricFamily(metric) for metric in self._metrics]

    def generate_latest(self) -> bytes:
        """Prometheus text exposition (версия 0.0.4)."""
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
        return ("\n".join(lines) + "\n").encode("utf-8")
