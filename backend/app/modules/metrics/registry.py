"""Prometheus-реестр метрик приложения (docs/01_ARCHITECTURE.md §9).

Метрики, требуемые TASK'ом:
    - ``http_request_duration_seconds`` — гистограмма латентности по endpoint;
    - ``arq_queue_length`` — длины очередей ARQ (parsing / llm);
    - ``queue_semaphore_slots_active`` — активные слоты Redis-семафора;
    - ``captcha_encounters_total`` — счётчик встреченных капч;
    - ``llm_execution_duration_seconds`` — гистограмма времени LLM-вызовов;
    - ``tasks_failed_total`` / ``task_errors_total`` — ошибки и отказы задач.

Используется ``prometheus_client``; зависимость опциональна — при её
отсутствии включается компактный встроенный реестр с тем же форматом
Prometheus text exposition (достаточно для ``curl /metrics``).
"""

from __future__ import annotations

import threading
from typing import Any

__all__ = [
    "CONTENT_TYPE",
    "HTTP_LATENCY_BUCKETS",
    "LLM_DURATION_BUCKETS",
    "captcha_rate",
    "inc_captcha",
    "inc_fetch_total",
    "inc_task_error",
    "inc_task_failure",
    "inc_task_success",
    "metrics_snapshot",
    "observe_http_request",
    "observe_llm_execution",
    "observe_task_outcome",
    "render_metrics",
    "reset_metrics",
    "set_alert_firing",
    "set_ollama_up",
    "set_queue_length",
    "set_semaphore_slots",
    "task_failure_rate",
    "totals",
]

#: Content-Type для Prometheus text exposition (совместим с v0.0.4).
CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"

#: Границы гистограммы латентности HTTP-запросов, секунды.
HTTP_LATENCY_BUCKETS: tuple[float, ...] = (
    0.005,
    0.01,
    0.025,
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
    10.0,
    30.0,
    60.0,
)

#: Границы гистограммы длительности LLM-вызовов, секунды.
LLM_DURATION_BUCKETS: tuple[float, ...] = (
    0.5,
    1.0,
    2.5,
    5.0,
    10.0,
    20.0,
    30.0,
    60.0,
    120.0,
    300.0,
    600.0,
)


def _build_metrics() -> Any:
    """Create metric objects from ``prometheus_client`` (or the fallback)."""
    counter_cls: Any
    gauge_cls: Any
    histogram_cls: Any
    registry_cls: Any
    try:
        from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

        counter_cls, gauge_cls = Counter, Gauge
        histogram_cls, registry_cls = Histogram, CollectorRegistry
    except Exception:  # noqa: BLE001 - prometheus_client is optional
        from app.modules.metrics._fallback import (
            Counter as FallbackCounter,
            Gauge as FallbackGauge,
            Histogram as FallbackHistogram,
            SimpleRegistry,
        )

        counter_cls, gauge_cls = FallbackCounter, FallbackGauge
        histogram_cls, registry_cls = FallbackHistogram, SimpleRegistry
    registry = registry_cls(auto_describe=True)
    http_latency = histogram_cls(
        "http_request_duration_seconds",
        "HTTP request latency in seconds",
        labelnames=("endpoint", "method"),
        buckets=HTTP_LATENCY_BUCKETS,
        registry=registry,
    )
    http_requests = counter_cls(
        "http_requests_total",
        "Total HTTP requests",
        labelnames=("endpoint", "method", "status"),
        registry=registry,
    )
    llm_duration = histogram_cls(
        "llm_execution_duration_seconds",
        "LLM execution duration in seconds",
        labelnames=("task_type",),
        buckets=LLM_DURATION_BUCKETS,
        registry=registry,
    )
    captcha_total = counter_cls(
        "captcha_encounters_total",
        "Captcha encounters during fetching",
        labelnames=("detector",),
        registry=registry,
    )
    fetch_total = counter_cls(
        "fetch_total",
        "Fetch attempts by result",
        labelnames=("result",),
        registry=registry,
    )
    tasks_completed = counter_cls(
        "tasks_completed_total",
        "Successfully completed tasks",
        labelnames=("task_type",),
        registry=registry,
    )
    tasks_failed = counter_cls(
        "tasks_failed_total",
        "Failed tasks (business failure)",
        labelnames=("task_type", "reason"),
        registry=registry,
    )
    task_errors = counter_cls(
        "task_errors_total",
        "Task errors (unexpected exception)",
        labelnames=("task_type",),
        registry=registry,
    )
    queue_length = gauge_cls(
        "arq_queue_length",
        "Pending jobs in ARQ queue",
        labelnames=("queue",),
        registry=registry,
    )
    semaphore_slots = gauge_cls(
        "queue_semaphore_slots_active",
        "Active Redis semaphore slots",
        labelnames=("group",),
        registry=registry,
    )
    ollama_up = gauge_cls(
        "ollama_up",
        "Ollama reachability (1 = up, 0 = down)",
        registry=registry,
    )
    alerts_firing = gauge_cls(
        "career_alert_firing",
        "1 when an alert threshold is exceeded",
        labelnames=("alert",),
        registry=registry,
    )
    return {
        "registry": registry,
        "http_latency": http_latency,
        "http_requests": http_requests,
        "llm_duration": llm_duration,
        "captcha_total": captcha_total,
        "fetch_total": fetch_total,
        "tasks_completed": tasks_completed,
        "tasks_failed": tasks_failed,
        "task_errors": task_errors,
        "queue_length": queue_length,
        "semaphore_slots": semaphore_slots,
        "ollama_up": ollama_up,
        "alerts_firing": alerts_firing,
    }


M = _build_metrics()

#: Реестр для доступа из внешних collectors (диагностика).
REGISTRY = M["registry"]

# --- Instrumentation helpers -----------------------------------------------------

#: In-process totals kept in sync with the Prometheus metrics, used for the
#: derived rates (captcha rate, task failure rate) and the JSON summary.
_totals: dict[str, float] = {
    "fetch_total": 0.0,
    "captcha_encounters_total": 0.0,
    "tasks_completed_total": 0.0,
    "tasks_failed_total": 0.0,
    "task_errors_total": 0.0,
    "http_requests_total": 0.0,
    "llm_execution_count": 0.0,
}
_totals_lock = threading.Lock()


def _bump(key: str, amount: float = 1.0) -> None:
    with _totals_lock:
        _totals[key] = _totals.get(key, 0.0) + float(amount)


def observe_http_request(
    endpoint: str, method: str, status_code: int, duration_seconds: float
) -> None:
    """Record HTTP request latency and count.

    ``endpoint`` must be the normalized route template (e.g. ``/api/v1/tasks``)
    to keep label cardinality bounded.
    """
    labels = {"endpoint": endpoint or "unknown", "method": (method or "GET").upper()}
    M["http_latency"].labels(**labels).observe(max(0.0, float(duration_seconds)))
    M["http_requests"].labels(**labels, status=str(status_code)).inc(1.0)
    _bump("http_requests_total")


def observe_llm_execution(task_type: str, duration_seconds: float) -> None:
    """Record LLM execution duration (label: task_type)."""
    M["llm_duration"].labels(task_type=task_type or "unknown").observe(
        max(0.0, float(duration_seconds))
    )
    _bump("llm_execution_count")


def inc_captcha(detector: str = "unknown") -> None:
    """Increment the captcha encounter counter (label: detector)."""
    M["captcha_total"].labels(detector=detector or "unknown").inc(1.0)
    _bump("captcha_encounters_total")


def inc_fetch_total(result: str) -> None:
    """Increment the fetch-attempt counter (result: ok|captcha|error)."""
    M["fetch_total"].labels(result=result or "unknown").inc(1.0)
    _bump("fetch_total")


def inc_task_success(task_type: str) -> None:
    """Increment the completed-task counter."""
    M["tasks_completed"].labels(task_type=task_type or "unknown").inc(1.0)
    _bump("tasks_completed_total")


def inc_task_failure(task_type: str, reason: str = "unknown") -> None:
    """Increment the failed-task counter (labels: task_type, reason)."""
    M["tasks_failed"].labels(task_type=task_type or "unknown", reason=reason or "unknown").inc(1.0)
    _bump("tasks_failed_total")


def inc_task_error(task_type: str) -> None:
    """Increment the unexpected-error counter for tasks."""
    M["task_errors"].labels(task_type=task_type or "unknown").inc(1.0)
    _bump("task_errors_total")


def observe_task_outcome(task_type: str, outcome: str, reason: str = "unknown") -> None:
    """Single entry point for task outcomes: completed | failed | error."""
    if outcome == "completed":
        inc_task_success(task_type)
    elif outcome == "failed":
        inc_task_failure(task_type, reason)
    elif outcome == "error":
        inc_task_error(task_type)


def set_queue_length(queue: str, length: int) -> None:
    """Set the number of pending jobs in an ARQ queue."""
    M["queue_length"].labels(queue=queue).set(max(0, int(length)))


def set_semaphore_slots(group: str, active: int) -> None:
    """Set the number of active Redis semaphore slots for a group."""
    M["semaphore_slots"].labels(group=group).set(max(0, int(active)))


def set_ollama_up(up: bool) -> None:
    """Set Ollama reachability (1 = up, 0 = down)."""
    M["ollama_up"].set(1 if up else 0)


def set_alert_firing(alert: str, firing: bool) -> None:
    """Set whether an alert threshold is currently exceeded (label: alert)."""
    M["alerts_firing"].labels(alert=alert).set(1 if firing else 0)


# --- Derived rates & rendering ---------------------------------------------------


def totals() -> dict[str, float]:
    """Copy of the in-process counters."""
    with _totals_lock:
        return dict(_totals)


def captcha_rate() -> float:
    """Ratio of captcha encounters to fetch attempts (0..1)."""
    with _totals_lock:
        fetches = _totals.get("fetch_total", 0.0)
        captchas = _totals.get("captcha_encounters_total", 0.0)
    if fetches <= 0:
        return 0.0
    return captchas / fetches


def task_failure_rate() -> float:
    """Ratio of failed/errored tasks to all finished tasks (0..1)."""
    with _totals_lock:
        completed = _totals.get("tasks_completed_total", 0.0)
        failed = _totals.get("tasks_failed_total", 0.0) + _totals.get("task_errors_total", 0.0)
    if completed + failed <= 0:
        return 0.0
    return failed / (completed + failed)


def metrics_snapshot() -> dict[str, Any]:
    """Aggregate snapshot for the JSON summary endpoint /metrics/summary."""
    snapshot: dict[str, Any] = {
        "captcha_rate": round(captcha_rate(), 4),
        "task_failure_rate": round(task_failure_rate(), 4),
    }
    snapshot.update({k: v for k, v in totals().items()})
    try:
        for metric in REGISTRY.collect():
            name = getattr(metric, "name", "")
            for sample in getattr(metric, "samples", []) or []:
                labels = dict(getattr(sample, "labels", {}) or {})
                if name == "arq_queue_length":
                    snapshot.setdefault("queues", {})[labels.get("queue", "unknown")] = sample.value
                elif name == "queue_semaphore_slots_active":
                    snapshot.setdefault("semaphore_slots", {})[labels.get("group", "unknown")] = (
                        sample.value
                    )
                elif name == "ollama_up":
                    snapshot["ollama_up"] = bool(sample.value)
                elif name == "career_alert_firing":
                    snapshot.setdefault("alerts", {})[labels.get("alert", "unknown")] = bool(
                        sample.value
                    )
    except Exception:  # noqa: BLE001 - never break the endpoint
        pass
    return snapshot


def render_metrics() -> bytes:
    """Render the Prometheus text exposition used by ``GET /metrics``."""
    try:
        from prometheus_client import generate_latest

        return generate_latest(REGISTRY)
    except Exception:  # noqa: BLE001 - fall back to the in-process registry
        if hasattr(REGISTRY, "generate_latest"):
            return REGISTRY.generate_latest()
        return b""


def reset_metrics() -> None:
    """Clear all recorded samples and counters (used by tests).

    ``prometheus_client`` has no API to zero a metric, so the collectors are
    re-created inside our own registry — the exposition then looks exactly like
    a freshly started process.
    """
    global M, REGISTRY
    with _totals_lock:
        for key in _totals:
            _totals[key] = 0.0
    try:
        REGISTRY.unregister_all()
    except Exception:  # noqa: BLE001 - fallback registry has no unregister_all
        pass
    M = _build_metrics()
    REGISTRY = M["registry"]
