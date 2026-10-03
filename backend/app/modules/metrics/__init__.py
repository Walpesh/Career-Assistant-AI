"""Metrics Module — Prometheus-метрики и пороги алертов (docs/01 §9).

Метрики:
    - ``http_request_duration_seconds`` — латентность HTTP по endpoint'ам;
    - ``arq_queue_length`` — длины очередей ARQ (parsing / llm);
    - ``queue_semaphore_slots_active`` — активные слоты Redis-семафора;
    - ``captcha_encounters_total`` — счётчик встреченных капч;
    - ``llm_execution_duration_seconds`` — гистограмма времени LLM-вызовов;
    - ``tasks_failed_total`` / ``task_errors_total`` — отказы и ошибки задач;
    - ``career_alert_firing`` — состояние порогов алертов.

Пороги алертов: рост LLM-очереди > 10 задач, доля капчи > 5%,
недоступность Ollama, доля отказов задач.
"""

from app.modules.metrics.alerts import (  # noqa: F401
    ALERT_CAPTCHA_RATE,
    ALERT_LLM_QUEUE_PENDING,
    ALERT_OLLAMA_DOWN,
    ALERT_TASK_FAILURE_RATE,
    alert_thresholds,
    evaluate_alerts,
    record_ollama_status,
    reset_alerts,
)
from app.modules.metrics.collector import (  # noqa: F401
    collect_metrics,
    start_collector,
    stop_collector,
)
from app.modules.metrics.middleware import MetricsMiddleware  # noqa: F401
from app.modules.metrics.registry import (  # noqa: F401
    CONTENT_TYPE,
    captcha_rate,
    inc_captcha,
    inc_fetch_total,
    inc_task_error,
    inc_task_failure,
    inc_task_success,
    metrics_snapshot,
    observe_http_request,
    observe_llm_execution,
    observe_task_outcome,
    render_metrics,
    reset_metrics,
    set_alert_firing,
    set_ollama_up,
    set_queue_length,
    set_semaphore_slots,
    task_failure_rate,
)
from app.modules.metrics.router import router  # noqa: F401
