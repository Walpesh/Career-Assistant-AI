"""Тесты Observability (docs/01_ARCHITECTURE.md §9).

Покрытие:
    - JSON-логирование (structlog) и санитизация PII/Authorization/Cookie;
    - request_id: HTTP-заголовок, WebSocket-сессия, очистка ввода;
    - Prometheus-эндпоинт GET /metrics и состав метрик;
    - пороги алертов: LLM-очередь > 10, капча > 5%, недоступность Ollama;
    - Sentry: no-op без DSN и PII-скраббинг события.
"""

from __future__ import annotations

import json
import logging

import pytest
from app.core.config import Settings
from app.core.logging import (
    MASKED,
    JsonFormatter,
    clear_request_id,
    configure_logging,
    get_logger,
    get_request_id,
    new_request_id,
    sanitize_headers,
    sanitize_payload,
    set_request_id,
)
from app.core.request_id import (
    REQUEST_ID_HEADER,
    RequestIDMiddleware,
    resolve_ws_request_id,
    sanitize_request_id,
)
from app.core.sentry import init_sentry, is_enabled, scrub_event
from app.main import create_app
from app.modules.metrics import alerts as alerts_module
from app.modules.metrics.registry import (
    captcha_rate,
    inc_captcha,
    inc_fetch_total,
    inc_task_failure,
    inc_task_success,
    metrics_snapshot,
    observe_http_request,
    observe_llm_execution,
    render_metrics,
    reset_metrics,
    set_alert_firing,
    set_ollama_up,
    set_queue_length,
    set_semaphore_slots,
    task_failure_rate,
)
from httpx import ASGITransport, AsyncClient

API = "/api/v1"


@pytest.fixture(autouse=True)
def _clean_observability_state():
    """Изолировать состояние метрик/алертов/request_id между тестами."""
    reset_metrics()
    alerts_module.reset_alerts()
    clear_request_id()
    yield
    reset_metrics()
    alerts_module.reset_alerts()
    clear_request_id()


# ============================================================
# Санитизация PII (docs/01 §9 — PII/токены не логируются)
# ============================================================


def test_sanitize_headers_masks_authorization_and_cookie():
    headers = sanitize_headers(
        {
            "Authorization": "Bearer eyJhbGciOiJIUzI1NiJ9.payload.signature",
            "Cookie": "ca_refresh_token=supersecret",
            "Set-Cookie": "session=abc",
            "X-Request-ID": "abc123",
        }
    )
    assert headers["Authorization"] == MASKED
    assert headers["Cookie"] == MASKED
    assert headers["Set-Cookie"] == MASKED
    assert headers["X-Request-ID"] == "abc123"


def test_sanitize_payload_masks_sensitive_keys_recursively():
    payload = sanitize_payload(
        {
            "user_id": 7,
            "jwt_secret": "super-secret",
            "nested": {"password": "p@ss", "refresh_token": "rt"},
            "email": "candidate@example.com",
        }
    )
    assert payload["jwt_secret"] == MASKED
    assert payload["nested"]["password"] == MASKED
    assert payload["nested"]["refresh_token"] == MASKED
    assert payload["email"] == MASKED
    assert payload["user_id"] == 7


def test_sanitize_payload_masks_pii_inside_free_text():
    note = "пиши на ivan.petrov@mail.ru или +7 999 123 45 67"
    serialized = json.dumps(sanitize_payload({"note": note}), ensure_ascii=False)
    assert "ivan.petrov@mail.ru" not in serialized
    assert "999 123 45 67" not in serialized


def test_sanitize_payload_keeps_non_sensitive_values():
    payload = sanitize_payload({"task_id": "abc", "duration": 1.5, "ok": True})
    assert payload == {"task_id": "abc", "duration": 1.5, "ok": True}


# ============================================================
# JSON-логирование
# ============================================================


def test_json_formatter_emits_single_json_line():
    configure_logging(level="INFO", json_output=True)
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="event happened",
        args=(),
        exc_info=None,
    )
    record.context = {"password": "secret", "user_id": 3}
    payload = json.loads(JsonFormatter().format(record))
    assert payload["event"] == "event happened"
    assert payload["level"] == "INFO"
    assert payload["password"] == MASKED
    assert payload["user_id"] == 3


def test_structured_logger_binds_request_id():
    configure_logging(level="INFO", json_output=True)
    set_request_id("req-1234")
    get_logger("test", user_id=1).info("something_happened")
    assert get_request_id() == "req-1234"


def test_request_id_generated_when_missing():
    clear_request_id()
    assert get_request_id() == ""
    generated = set_request_id()
    assert generated == get_request_id()
    assert len(generated) == len(new_request_id())


# ============================================================
# request_id в HTTP и WebSocket
# ============================================================


def test_sanitize_request_id_strips_unsafe_characters():
    assert sanitize_request_id("abc-123_x") == "abc-123_x"
    assert sanitize_request_id("bad\r\nX-Injected: 1") == "badX-Injected1"
    assert len(sanitize_request_id("a" * 200)) == 64


def test_resolve_ws_request_id_from_query_and_headers():
    assert resolve_ws_request_id({"request_id": "ws-42"}) == "ws-42"
    assert get_request_id() == "ws-42"
    assert resolve_ws_request_id({}, {"x-request-id": "ws-99"}) == "ws-99"
    assert len(resolve_ws_request_id({}, {})) == 16


async def test_health_response_contains_request_id_header():
    app = create_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.get("/health", headers={REQUEST_ID_HEADER: "trace-abc"})
    assert response.status_code == 200
    assert response.headers[REQUEST_ID_HEADER] == "trace-abc"


async def test_request_id_generated_when_header_absent():
    app = create_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.get("/health")
    assert len(response.headers[REQUEST_ID_HEADER]) == 16


def test_request_id_middleware_is_registered():
    app = create_app()
    names = {getattr(middleware, "cls", None) for middleware in app.user_middleware}
    assert RequestIDMiddleware in names


# ============================================================
# Prometheus: GET /metrics
# ============================================================


def test_render_metrics_exposes_required_metric_names():
    observe_http_request(f"{API}/tasks", "GET", 200, 0.25)
    observe_llm_execution("analyze", 2.5)
    inc_fetch_total("ok")
    inc_captcha("cloudflare")
    inc_task_failure("analyze", "llm_error")
    set_queue_length("llm", 3)
    set_semaphore_slots("llm", 1)
    set_ollama_up(True)

    payload = render_metrics().decode("utf-8")
    for name in (
        "http_request_duration_seconds",
        "llm_execution_duration_seconds",
        "captcha_encounters_total",
        "arq_queue_length",
        "queue_semaphore_slots_active",
        "tasks_failed_total",
        "task_errors_total",
        "ollama_up",
    ):
        assert name in payload


async def test_metrics_endpoint_returns_prometheus_exposition():
    app = create_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.get("/metrics")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert "# HELP http_request_duration_seconds" in response.text


async def test_metrics_endpoint_records_http_latency_of_the_call():
    app = create_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        await client.get("/metrics")
        response = await client.get("/metrics")
    assert 'http_requests_total{endpoint="/metrics"' in response.text


async def test_metrics_summary_endpoint():
    inc_fetch_total("captcha")
    inc_fetch_total("ok")
    inc_captcha("cloudflare")
    set_queue_length("llm", 4)
    app = create_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.get("/metrics/summary")
    assert response.status_code == 200
    body = response.json()
    assert body["queues"]["llm"] == 4
    assert body["captcha_rate"] == pytest.approx(0.5)


async def test_metrics_alerts_endpoint_returns_thresholds():
    app = create_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.get("/metrics/alerts")
    assert response.status_code == 200
    body = response.json()
    assert body["thresholds"]["llm_queue_pending"] == 10
    assert body["thresholds"]["captcha_rate"] == pytest.approx(0.05)
    assert body["firing"] == []


def test_metrics_can_be_disabled_by_settings():
    assert Settings(metrics_enabled=False, _env_file=None).metrics_enabled is False


# ============================================================
# Производные rate
# ============================================================


def test_captcha_rate_zero_without_fetches():
    assert captcha_rate() == 0.0


def test_captcha_rate_computed_from_counters():
    for _ in range(19):
        inc_fetch_total("ok")
    inc_fetch_total("captcha")
    inc_captcha("cloudflare")
    assert captcha_rate() == pytest.approx(0.05)


def test_task_failure_rate_computed_from_outcomes():
    inc_task_success("analyze")
    for _ in range(3):
        inc_task_failure("analyze", "llm_error")
    assert task_failure_rate() == pytest.approx(0.75)


def test_metrics_snapshot_contains_gauges():
    set_queue_length("parsing", 2)
    set_semaphore_slots("parsing", 1)
    set_alert_firing("captcha_rate", True)
    snapshot = metrics_snapshot()
    assert snapshot["queues"]["parsing"] == 2
    assert snapshot["semaphore_slots"]["parsing"] == 1
    assert snapshot["alerts"]["captcha_rate"] is True


# ============================================================
# Пороги алертов
# ============================================================


def test_llm_queue_growth_alert_fires_above_ten_pending():
    report = alerts_module.evaluate_alerts({"queues": {"llm": 11}, "captcha_rate": 0.0})
    assert alerts_module.ALERT_LLM_QUEUE_PENDING in report["firing"]
    assert alerts_module.ALERT_LLM_QUEUE_PENDING in metrics_snapshot()["alerts"]


def test_llm_queue_alert_silent_at_threshold():
    report = alerts_module.evaluate_alerts({"queues": {"llm": 10}, "captcha_rate": 0.0})
    assert alerts_module.ALERT_LLM_QUEUE_PENDING not in report["firing"]


def test_captcha_rate_alert_fires_above_five_percent():
    report = alerts_module.evaluate_alerts({"queues": {"llm": 0}, "captcha_rate": 0.06})
    assert alerts_module.ALERT_CAPTCHA_RATE in report["firing"]


def test_captcha_rate_alert_silent_below_threshold():
    report = alerts_module.evaluate_alerts({"queues": {"llm": 0}, "captcha_rate": 0.04})
    assert alerts_module.ALERT_CAPTCHA_RATE not in report["firing"]


def test_ollama_unreachable_alert_fires_after_grace_period(monkeypatch):
    monkeypatch.setattr(
        alerts_module,
        "settings",
        Settings(alert_ollama_down_seconds=0, _env_file=None),
    )
    alerts_module.record_ollama_status(False)
    report = alerts_module.evaluate_alerts({"queues": {"llm": 0}, "captcha_rate": 0.0})
    assert alerts_module.ALERT_OLLAMA_DOWN in report["firing"]


def test_ollama_recovery_clears_alert():
    alerts_module.record_ollama_status(False)
    alerts_module.record_ollama_status(True)
    report = alerts_module.evaluate_alerts({"queues": {"llm": 0}, "captcha_rate": 0.0})
    assert alerts_module.ALERT_OLLAMA_DOWN not in report["firing"]


def test_alert_thresholds_come_from_config():
    thresholds = alerts_module.alert_thresholds()
    assert thresholds["llm_queue_pending"] == 10
    assert thresholds["captcha_rate"] == pytest.approx(0.05)


# ============================================================
# Sentry
# ============================================================


def test_sentry_is_noop_without_dsn():
    assert init_sentry(dsn="", environment="test") is False
    assert is_enabled() is False


def test_scrub_event_removes_pii_from_request_and_extra():
    event = {
        "request": {
            "url": "https://testserver/api/v1/auth/login?token=abc123",
            "headers": {"Authorization": "Bearer aaa.bbb.ccc", "Cookie": "s=1"},
            "cookies": {"ca_refresh_token": "secret"},
            "data": {"email": "candidate@example.com", "password": "p"},
            "query_string": "token=abc123",
        },
        "extra": {"jwt_secret": "top", "task_id": "abc"},
        "user": {
            "id": "u-1",
            "email": "candidate@example.com",
            "ip_address": "1.2.3.4",
        },
        "breadcrumbs": {"values": [{"message": "mail a@b.ru", "data": {"phone": "+79991234567"}}]},
    }
    scrubbed = scrub_event(event)

    assert scrubbed["request"]["headers"]["Authorization"] == MASKED
    assert scrubbed["request"]["headers"]["Cookie"] == MASKED
    assert scrubbed["request"]["cookies"] == MASKED
    assert scrubbed["request"]["data"]["email"] == MASKED
    assert scrubbed["request"]["query_string"] == MASKED
    assert "token=***MASKED***" in scrubbed["request"]["url"]
    assert scrubbed["extra"]["jwt_secret"] == MASKED
    assert scrubbed["extra"]["task_id"] == "abc"
    assert scrubbed["user"] == {"id": "u-1"}
    assert "a@b.ru" not in scrubbed["breadcrumbs"]["values"][0]["message"]
    assert scrubbed["breadcrumbs"]["values"][0]["data"]["phone"] == MASKED
