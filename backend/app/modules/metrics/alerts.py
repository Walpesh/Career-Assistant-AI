"""Пороги алертов Observability (docs/01_ARCHITECTURE.md §9).

Требуемые TASK'ом пороги:
    1. Рост LLM-очереди — больше ``alert_llm_queue_pending_threshold`` (10)
       ожидающих задач в очереди ``career:queue:llm``;
    2. Доля капчи — выше ``alert_captcha_rate_threshold`` (5%) ответов
       hh.ru сопровождается капчей;
    3. Недоступность Ollama — LLM недоступен дольше
       ``alert_ollama_down_seconds`` (60 с);
    4. Доля отказов задач — выше ``alert_task_failure_rate_threshold``.

Каждый порог публикуется как Prometheus-метрика ``career_alert_firing``
(1 — порог превышен), а нарушение логируется в JSON-лог и отправляется в
Sentry (если он настроен). Значения порогов берутся из ``app.core.config``.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from app.core.config import settings
from app.core.logging import get_logger
from app.core.sentry import capture_message
from app.modules.metrics.registry import (
    captcha_rate,
    metrics_snapshot,
    set_alert_firing,
    task_failure_rate,
)

__all__ = [
    "ALERT_CAPTCHA_RATE",
    "ALERT_LLM_QUEUE_PENDING",
    "ALERT_OLLAMA_DOWN",
    "ALERT_TASK_FAILURE_RATE",
    "AlertState",
    "alert_thresholds",
    "evaluate",
    "evaluate_alerts",
    "record_ollama_status",
    "reset_alerts",
]

logger = get_logger(__name__)

#: Идентификаторы алертов (используются как label ``alert``).
ALERT_LLM_QUEUE_PENDING = "llm_queue_pending"
ALERT_CAPTCHA_RATE = "captcha_rate"
ALERT_OLLAMA_DOWN = "ollama_down"
ALERT_TASK_FAILURE_RATE = "task_failure_rate"

#: Короткое имя LLM-очереди в метриках (без префикса Redis).
LLM_QUEUE_METRIC_NAME = "llm"


@dataclass
class AlertState:
    """Состояние алертов процесса (для дедупликации Sentry-уведомлений)."""

    firing: dict[str, bool] = field(default_factory=dict)
    ollama_down_since: float | None = None


_state = AlertState()


def alert_thresholds() -> dict[str, float]:
    """Текущие пороги из конфигурации приложения."""
    return {
        "llm_queue_pending": float(settings.alert_llm_queue_pending_threshold),
        "captcha_rate": float(settings.alert_captcha_rate_threshold),
        "task_failure_rate": float(settings.alert_task_failure_rate_threshold),
        "ollama_down_seconds": float(settings.alert_ollama_down_seconds),
    }


def reset_alerts() -> None:
    """Сбросить состояние алертов (используется в тестах)."""
    global _state
    _state = AlertState()
    for alert in (
        ALERT_LLM_QUEUE_PENDING,
        ALERT_CAPTCHA_RATE,
        ALERT_OLLAMA_DOWN,
        ALERT_TASK_FAILURE_RATE,
    ):
        set_alert_firing(alert, False)


def record_ollama_status(up: bool) -> float | None:
    """Обновить доступность Ollama; вернуть момент начала простоя."""
    now = time.monotonic()
    if up:
        _state.ollama_down_since = None
        return None
    if _state.ollama_down_since is None:
        _state.ollama_down_since = now
    return _state.ollama_down_since


def _fire(alert: str, message: str, **fields: Any) -> None:
    """Отметить алерт сработавшим: метрика + JSON-лог + Sentry (без спама)."""
    set_alert_firing(alert, True)
    if _state.firing.get(alert):
        return  # уже сообщали — повторно не шлём
    _state.firing[alert] = True
    logger.warning("alert_firing", alert=alert, message=message, **fields)
    capture_message(f"{alert}: {message}", level="warning")


def _clear(alert: str, **fields: Any) -> None:
    """Отметить алерт восстановленным."""
    set_alert_firing(alert, False)
    if _state.firing.get(alert):
        _state.firing[alert] = False
        logger.info("alert_resolved", alert=alert, **fields)


def evaluate_alerts(snapshot: dict[str, Any] | None = None) -> dict[str, Any]:
    """Проверить все пороги; вернуть отчёт о состоянии алертов.

    Args:
        snapshot: предвычисленный снимок метрик (по умолчанию — из registry).

    Returns:
        ``{"firing": [...], "values": {...}, "thresholds": {...}}``.
    """
    data = snapshot if snapshot is not None else metrics_snapshot()
    thresholds = alert_thresholds()
    firing: list[str] = []

    # 1) Рост LLM-очереди: pending-задач больше порога.
    llm_pending = int((data.get("queues") or {}).get(LLM_QUEUE_METRIC_NAME, 0) or 0)
    if llm_pending > thresholds["llm_queue_pending"]:
        firing.append(ALERT_LLM_QUEUE_PENDING)
        _fire(
            ALERT_LLM_QUEUE_PENDING,
            f"LLM queue has {llm_pending} pending tasks "
            f"(threshold {int(thresholds['llm_queue_pending'])})",
            pending=llm_pending,
            threshold=thresholds["llm_queue_pending"],
        )
    else:
        _clear(ALERT_LLM_QUEUE_PENDING, pending=llm_pending)

    # 2) Доля капчи выше порога (5% по умолчанию).
    current_captcha_rate = float(data.get("captcha_rate") or 0.0)
    if current_captcha_rate > thresholds["captcha_rate"]:
        firing.append(ALERT_CAPTCHA_RATE)
        _fire(
            ALERT_CAPTCHA_RATE,
            f"Captcha rate {current_captcha_rate:.1%} exceeds "
            f"threshold {thresholds['captcha_rate']:.1%}",
            captcha_rate=current_captcha_rate,
            threshold=thresholds["captcha_rate"],
        )
    else:
        _clear(ALERT_CAPTCHA_RATE, captcha_rate=current_captcha_rate)

    # 3) Доля отказов задач выше порога.
    current_failure_rate = float(data.get("task_failure_rate") or 0.0)
    if current_failure_rate > thresholds["task_failure_rate"]:
        firing.append(ALERT_TASK_FAILURE_RATE)
        _fire(
            ALERT_TASK_FAILURE_RATE,
            f"Task failure rate {current_failure_rate:.1%} exceeds "
            f"threshold {thresholds['task_failure_rate']:.1%}",
            task_failure_rate=current_failure_rate,
            threshold=thresholds["task_failure_rate"],
        )
    else:
        _clear(ALERT_TASK_FAILURE_RATE, task_failure_rate=current_failure_rate)

    # 4) Недоступность Ollama дольше alert_ollama_down_seconds.
    down_since = _state.ollama_down_since
    ollama_down_seconds = 0.0 if down_since is None else time.monotonic() - down_since
    if ollama_down_seconds > thresholds["ollama_down_seconds"]:
        firing.append(ALERT_OLLAMA_DOWN)
        _fire(
            ALERT_OLLAMA_DOWN,
            f"Ollama unreachable for {ollama_down_seconds:.0f}s "
            f"(threshold {thresholds['ollama_down_seconds']:.0f}s)",
            down_seconds=round(ollama_down_seconds, 1),
            threshold=thresholds["ollama_down_seconds"],
        )
    else:
        _clear(ALERT_OLLAMA_DOWN)

    return {
        "firing": firing,
        "values": {
            "llm_queue_pending": llm_pending,
            "captcha_rate": round(current_captcha_rate, 4),
            "task_failure_rate": round(current_failure_rate, 4),
            "ollama_down_seconds": round(ollama_down_seconds, 1),
        },
        "thresholds": thresholds,
    }


def evaluate() -> dict[str, Any]:
    """Обертка :func:`evaluate_alerts` с пересчётом производных rate."""
    return evaluate_alerts(
        {
            **metrics_snapshot(),
            "captcha_rate": captcha_rate(),
            "task_failure_rate": task_failure_rate(),
        }
    )
