"""Юнит-тесты: Proxy & Anti-Ban Module (docs/04_PARSING_RULES.md §1, §3, §5, §9).

Покрытие:
    - задержки 4–8 с (нормальное распределение) — §1;
    - согласованность отпечатков UA ↔ sec-ch-ua ↔ Sec-Fetch-* — §3.3;
    - ротация sticky-сессий (5–10 мин / 60–80 запросов), failover, cooldown — §3.2;
    - валидация «только residential» и сборка endpoints из .env — §3.1;
    - детект 429 / Cloudflare / hh-капчи / серии 404, backoff — §5;
    - прогрев на https://hh.ru/, cookies, retry-цикл сессии — §3.3;
    - human-mimicry (Безье/скролл/паузы) — §3.3; доля капчи > 5% — §9.

Сеть и реальное ожидание не используются: fetcher/sleeper/clock внедряются.
"""

from __future__ import annotations

import random

import pytest

from app.core.config import Settings
from app.modules.anti_ban import (
    AntiBanSession,
    CaptchaDetected,
    FetchResponse,
    PreparedRequest,
    ProxyEndpoint,
    ProxyExhaustedError,
    ProxyRotator,
    ThreatKind,
    backoff_delay,
    build_endpoints,
    captcha_rate_exceeded,
    classify_response,
    detect_captcha,
    detect_rate_limit,
    detect_throttling,
    generate_fingerprint,
    human_sleep,
    next_delay,
    random_mouse_path,
    reading_pause,
    scroll_deltas,
    validate_residential,
    warmup_steps,
)
from app.modules.anti_ban.constants import (
    CONSECUTIVE_404_THRESHOLD,
    DELAY_MAX_SECONDS,
    DELAY_MIN_SECONDS,
    REQUESTS_PER_IP_MAX,
    REQUESTS_PER_IP_MIN,
    STICKY_SESSION_MAX_SECONDS,
    STICKY_SESSION_MIN_SECONDS,
    THROTTLING_PAUSE_MAX_SECONDS,
    THROTTLING_PAUSE_MIN_SECONDS,
    WARMUP_URL,
)
from app.modules.anti_ban.exceptions import ProxyConfigError, ProxyError

VACANCY_URL = "https://ekaterinburg.hh.ru/vacancy/137866214"

CF_HTML = (
    "<html><head><title>Just a moment…</title></head>"
    '<body><div id="challenge-platform"></div></body></html>'
)


class FakeClock:
    """Управляемые часы для тестов sticky-сессий (monotonic-совместимые)."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def make_endpoints(*labels: str) -> list[ProxyEndpoint]:
    return [
        ProxyEndpoint(
            url=f"http://user_{label}:pass@{label}.proxy.local:9000",
            provider="brightdata",
        )
        for label in labels
    ]


def make_rotator(labels=("a", "b", "c"), *, seed=7, clock=None, **kwargs) -> ProxyRotator:
    return ProxyRotator(
        make_endpoints(*labels),
        rng=random.Random(seed),
        clock=clock or FakeClock(),
        **kwargs,
    )


def make_recorder_sleeper():
    """sleeper, записывающий паузы вместо реального ожидания."""
    sleeps: list[float] = []

    async def sleeper(seconds: float) -> None:
        sleeps.append(float(seconds))

    return sleeps, sleeper


def make_fetcher(responses: list):
    """fetcher: элементы списка выдаются по порядку (ответ или Exception)."""
    calls: list[PreparedRequest] = []

    async def fetch(request: PreparedRequest):
        calls.append(request)
        item = responses[min(len(calls) - 1, len(responses) - 1)]
        if isinstance(item, Exception):
            raise item
        return item

    return calls, fetch


# ==================================================== docs/04 §1 — задержки 4–8 с


def test_next_delay_within_4_8_and_normal_mean():
    rng = random.Random(11)
    samples = [next_delay(rng) for _ in range(500)]
    assert all(DELAY_MIN_SECONDS <= value <= DELAY_MAX_SECONDS for value in samples)
    mean = sum(samples) / len(samples)
    assert 5.7 <= mean <= 6.3  # нормальное распределение, μ = 6 с


async def test_human_sleep_uses_injected_sleeper():
    sleeps, sleeper = make_recorder_sleeper()
    waited = await human_sleep(rng=random.Random(1), sleeper=sleeper)
    assert len(sleeps) == 1 and sleeps[0] == waited
    assert DELAY_MIN_SECONDS <= waited <= DELAY_MAX_SECONDS


# ====================================== docs/04 §3.3 — отпечатки (UA/заголовки)


def test_fingerprint_headers_are_consistent():
    rng = random.Random(42)
    seen_browsers: set[str] = set()
    for _ in range(60):
        fp = generate_fingerprint(rng)
        seen_browsers.add(fp.browser)
        headers = fp.headers()
        assert headers["User-Agent"] == fp.user_agent
        assert headers["Accept-Language"].startswith("ru-RU")
        assert headers["Sec-Fetch-Dest"] == "document"
        assert headers["Sec-Fetch-Mode"] == "navigate"
        if fp.browser == "chrome":
            # версия Client Hints обязана совпадать с версией в UA
            assert f'v="{fp.major_version}"' in headers["sec-ch-ua"]
            assert headers["sec-ch-ua-platform"] == f'"{fp.sec_ch_ua_platform}"'
            assert f"Chrome/{fp.major_version}." in fp.user_agent
        else:  # Firefox/Safari не шлют Client Hints — как в реальных браузерах
            assert "sec-ch-ua" not in headers
    assert seen_browsers == {"chrome", "firefox", "safari"}  # все профили ротируются


def test_headers_depend_on_request_dest():
    fp = generate_fingerprint(random.Random(3))
    document = fp.headers("document")
    xhr = fp.headers("empty")
    assert document["Sec-Fetch-User"] == "?1"
    assert "Upgrade-Insecure-Requests" in document
    assert xhr["Sec-Fetch-Mode"] == "same-origin"
    assert xhr["Sec-Fetch-Dest"] == "empty"
    assert "Sec-Fetch-User" not in xhr
    assert "Upgrade-Insecure-Requests" not in xhr


# ======================================== docs/04 §3.1 — только residential-прокси


def test_datacenter_and_missing_provider_rejected():
    with pytest.raises(ProxyConfigError):
        validate_residential("")
    with pytest.raises(ProxyConfigError):
        validate_residential("some-datacenter")
    cfg = Settings(
        proxy_provider="dc-hosting",
        proxy_list="http://user:pass@dc.example:8080",
    )
    with pytest.raises(ProxyConfigError):
        build_endpoints(cfg)


def test_empty_config_is_direct_mode():
    assert build_endpoints(Settings()) == ()


def test_residential_proxy_list_parsed():
    cfg = Settings(
        proxy_provider="BrightData",
        proxy_list="http://u:p@exit1.example:9000, socks5://u:p@exit2.example:9001",
    )
    endpoints = build_endpoints(cfg)
    assert len(endpoints) == 2
    assert all(endpoint.provider == "brightdata" for endpoint in endpoints)


def test_gateway_rotates_session_token():
    cfg = Settings(
        proxy_provider="smartproxy",
        proxy_gateway="gate.example:9000",
        proxy_username="user",
        proxy_password="pass",
    )
    endpoints = build_endpoints(cfg)
    assert len(endpoints) == 1 and endpoints[0].template
    rotator = ProxyRotator(endpoints, rng=random.Random(1))
    first = rotator.acquire()
    second = rotator.rotate()
    assert "session-" in first.url  # смена токена = смена exit IP (§3.2)
    assert first.url != second.url


# ======================================== docs/04 §3.2 — ротация sticky-сессий


def test_rotation_after_sticky_lifetime_5_10_minutes():
    clock = FakeClock()
    rotator = make_rotator(("a", "b"), clock=clock)
    first = rotator.acquire()

    clock.advance(STICKY_SESSION_MIN_SECONDS - 1)  # 299 с < минимума 300 с
    assert rotator.acquire() is first
    assert rotator.rotations == 1

    clock.advance(STICKY_SESSION_MAX_SECONDS)  # 600+ с ≥ максимума 600 с (§3.2)
    second = rotator.acquire()
    assert second is not first
    assert rotator.rotations == 2
    assert rotator.last_reason == "lifetime"


def test_rotation_after_60_80_requests():
    rotator = make_rotator(("a", "b", "c"))
    rotator.acquire()
    boundary: int | None = None
    for issued in range(1, REQUESTS_PER_IP_MAX + 1):
        rotator.record_request()
        rotator.acquire()  # ленивая проверка лимитов перед следующим запросом
        if rotator.rotations > 1:
            boundary = issued
            break
    assert boundary is not None, "ротация не наступила в диапазоне 60–80 запросов"
    assert REQUESTS_PER_IP_MIN <= boundary <= REQUESTS_PER_IP_MAX
    assert rotator.last_reason == "requests"


def test_failover_skips_dead_proxy():
    rotator = make_rotator(("a", "b", "c"))
    first = rotator.acquire()
    replacement = rotator.report_failure(first)

    assert replacement.label != first.label  # немедленный failover (§3.2)
    for _ in range(6):  # во всех последующих циклах мёртвый IP не возвращается
        rotator.rotate()
        assert rotator.current.label != first.label


def test_exhausted_pool_raises_and_revive_restores():
    rotator = make_rotator(("a", "b"))
    first = rotator.acquire()
    rotator.report_failure(first)  # a мёртв → failover на b

    with pytest.raises(ProxyExhaustedError):
        rotator.report_failure(rotator.current)  # b тоже мёртв → пул исчерпан

    rotator.revive()  # ручной сброс — пул живёт снова
    assert rotator.acquire() is not None


def test_rate_limit_applies_cooldown_and_switches_ip():
    clock = FakeClock()
    rotator = make_rotator(("a", "b"), clock=clock)
    first = rotator.acquire()

    replacement = rotator.report_rate_limit(pause_seconds=60.0)
    assert replacement.label != first.label  # немедленная смена IP (§3.2)

    next_one = rotator.rotate()
    assert next_one.label != first.label  # first ещё в cooldown 60 с

    clock.advance(61.0)  # пауза прошла → IP снова доступен
    assert rotator.rotate().label == first.label


# ================================== docs/04 §5 — детект 429 / капчи / троттлинга


def test_detect_rate_limit_429_and_retry_after():
    assert detect_rate_limit(429, {"Retry-After": "12"})
    assert not detect_rate_limit(200, {}, "<html>вакансия</html>")

    detection = classify_response(429, {"Retry-After": "12"})
    assert detection.kind is ThreatKind.RATE_LIMITED
    assert detection.should_rotate  # §3.2: немедленная смена IP
    assert detection.task_status == "retry"  # docs/04 §5
    assert detection.retry_after == 12.0
    assert detection.should_retry


def test_detect_cloudflare_and_hh_captcha():
    assert detect_captcha(503, {}, CF_HTML)  # Cloudflare interstitial
    assert detect_captcha(403, {}, "smartcaptcha: пройдите проверку")  # hh SmartCaptcha
    assert detect_captcha(200, {}, "", url="https://hh.ru/captcha?back=1")  # редирект
    # обычный HTML со словом captcha НЕ является капчей (защита от ложных срабатываний)
    assert not detect_captcha(200, {}, "скрипт smartcaptcha подключён на странице")

    detection = classify_response(503, {}, CF_HTML)
    assert detection.kind is ThreatKind.CAPTCHA
    assert detection.task_status == "waiting_captcha"  # docs/04 §5
    assert detection.should_rotate  # §3.2: смена IP + остановка воркера


def test_consecutive_404_becomes_throttling():
    threshold = CONSECUTIVE_404_THRESHOLD  # docs/04 §5: «серия 404»
    assert not detect_throttling(threshold - 1)
    assert detect_throttling(threshold)

    unit = classify_response(404, consecutive_404=0)
    assert unit.kind is ThreatKind.NOT_FOUND  # удалённая вакансия — не серия
    assert unit.task_status == "not_found"
    assert not unit.should_rotate

    series = classify_response(404, consecutive_404=threshold - 1)
    assert series.kind is ThreatKind.THROTTLING
    assert series.should_rotate  # пауза 30–60 с + смена IP (§5)
    assert series.task_status == "retry"


def test_backoff_delay_exponential_with_cap_and_retry_after():
    assert backoff_delay(1) == 8.0  # база = максимуму задержки §1 (8 с)
    assert backoff_delay(2) == 16.0
    assert backoff_delay(3) == 32.0
    assert backoff_delay(20) == 300.0  # потолок
    assert backoff_delay(1, retry_after=120.0) == 120.0  # Retry-After важнее
    # джиттер ±10% при наличии rng
    assert 7.2 <= backoff_delay(1, rng=random.Random(5)) <= 8.8


def test_captcha_rate_threshold_5_percent():
    assert not captcha_rate_exceeded(100, 5)  # ровно 5% — не превышение
    assert captcha_rate_exceeded(100, 6)  # > 5% → снижать интенсивность (§9)
    assert not captcha_rate_exceeded(0, 0)


# ==================================== docs/04 §3.3 — human-mimicry (Playwright)


def test_human_mimicry_parameters():
    rng = random.Random(5)
    path = random_mouse_path((0.0, 0.0), (400.0, 300.0), rng=rng, steps=25)
    assert len(path) == 25
    assert path[0] == (0.0, 0.0)  # мышь начинается ровно в исходной точке
    assert path[-1] == (400.0, 300.0)  # и заканчивается ровно в целевой

    deltas = scroll_deltas(rng)
    assert 3 <= len(deltas) <= 6
    assert all(80 <= delta <= 320 for delta in deltas)

    pause = reading_pause(rng)
    assert 0.4 <= pause <= 1.6  # «небольшие паузы чтения» (§3.3)


# ============================== docs/04 §3.3/§5 — сессия: прогрев и retry-цикл


async def test_session_warms_homepage_first_and_reuses_cookies():
    rotator = make_rotator(("a", "b"))
    sleeps, sleeper = make_recorder_sleeper()
    session = AntiBanSession(rotator, rng=random.Random(9), sleeper=sleeper)
    assert warmup_steps() == [WARMUP_URL]  # план прогрева (§3.3)

    calls, fetch = make_fetcher(
        [
            FetchResponse(200, headers={"set-cookie": "sid=abc123; Path=/"}),
            FetchResponse(200, url=VACANCY_URL),
            FetchResponse(200, url=VACANCY_URL),
        ]
    )
    response = await session.fetch(VACANCY_URL, fetcher=fetch)
    assert response.status_code == 200
    assert calls[0].url == WARMUP_URL  # первый запрос сессии — всегда homepage
    assert calls[1].url == VACANCY_URL
    assert session.warmed_up
    assert session.cookies == {"sid": "abc123"}  # cookies живут в сессии (§3.3)
    assert calls[1].headers["Cookie"] == "sid=abc123"
    # human-пауза 4–8 с перед целевым запросом (§1); перед прогревом её нет
    assert len(sleeps) == 1 and DELAY_MIN_SECONDS <= sleeps[0] <= DELAY_MAX_SECONDS

    second = await session.fetch(VACANCY_URL, fetcher=fetch)
    assert second.status_code == 200
    assert calls[2].url == VACANCY_URL  # прогрев не повторяется
    assert len(sleeps) == 2


async def test_rate_limit_rotates_proxy_and_backs_off():
    rotator = make_rotator(("a", "b"))
    sleeps, sleeper = make_recorder_sleeper()
    session = AntiBanSession(rotator, rng=random.Random(9), sleeper=sleeper)
    calls, fetch = make_fetcher(
        [
            FetchResponse(200),  # прогрев
            FetchResponse(429, headers={"Retry-After": "7"}),
            FetchResponse(200, url=VACANCY_URL),
        ]
    )
    response = await session.fetch(VACANCY_URL, fetcher=fetch)
    assert response.status_code == 200
    assert len(calls) == 3  # прогрев → 429 → повтор
    assert calls[2].proxy_url != calls[1].proxy_url  # смена IP (§3.2)
    # human-пауза 4–8 с + exponential backoff не меньше Retry-After (§1, §5)
    human, backoff = sleeps[0], sleeps[1]
    assert DELAY_MIN_SECONDS <= human <= DELAY_MAX_SECONDS
    assert backoff >= 7.0


async def test_proxy_connection_error_triggers_failover():
    rotator = make_rotator(("a", "b"))
    _, sleeper = make_recorder_sleeper()
    session = AntiBanSession(rotator, rng=random.Random(9), sleeper=sleeper)
    calls, fetch = make_fetcher(
        [
            FetchResponse(200),  # прогрев
            ProxyError("connection refused"),  # прокси мёртв
            FetchResponse(200, url=VACANCY_URL),  # повтор уже через живой IP
        ]
    )
    response = await session.fetch(VACANCY_URL, fetcher=fetch)
    assert response.status_code == 200
    assert len(calls) == 3
    assert calls[2].proxy_url != calls[1].proxy_url  # failover (§3.2)


async def test_captcha_stops_worker_with_waiting_status():
    rotator = make_rotator(("a", "b"))
    _, sleeper = make_recorder_sleeper()
    session = AntiBanSession(rotator, rng=random.Random(9), sleeper=sleeper)
    calls, fetch = make_fetcher(
        [
            FetchResponse(200),  # прогрев
            FetchResponse(503, text=CF_HTML),  # Cloudflare challenge
        ]
    )
    with pytest.raises(CaptchaDetected) as exc_info:
        await session.fetch(VACANCY_URL, fetcher=fetch)
    assert exc_info.value.task_status == "waiting_captcha"  # docs/04 §5
    assert len(calls) == 2  # без повторов — воркер остановлен
    assert session.captcha_count == 1
    assert session.captcha_rate_exceeded  # 1 из 2 > 5% → снижать (§9)


async def test_series_of_404_triggers_pause_and_ip_change():
    rotator = make_rotator(("a", "b"))
    sleeps, sleeper = make_recorder_sleeper()
    session = AntiBanSession(rotator, rng=random.Random(9), sleeper=sleeper)
    calls, fetch = make_fetcher(
        [
            FetchResponse(200),  # прогрев
            FetchResponse(404, url=VACANCY_URL),  # 1-й 404 → not_found
            FetchResponse(404, url=VACANCY_URL),  # 2-й 404 → not_found
            FetchResponse(404, url=VACANCY_URL),  # 3-й подряд → троттлинг (§5)
            FetchResponse(200, url=VACANCY_URL),  # после паузы и смены IP
        ]
    )
    first = await session.fetch(VACANCY_URL, fetcher=fetch)  # idx 1
    assert first.status_code == 404 and session.consecutive_404 == 1
    second = await session.fetch(VACANCY_URL, fetcher=fetch)  # idx 2
    assert second.status_code == 404 and session.consecutive_404 == 2
    third = await session.fetch(VACANCY_URL, fetcher=fetch)  # idx 3 → пауза → idx 4
    assert third.status_code == 200

    pauses = [
        value
        for value in sleeps
        if THROTTLING_PAUSE_MIN_SECONDS <= value <= THROTTLING_PAUSE_MAX_SECONDS
    ]
    assert pauses, "после серии 404 обязана быть пауза 30–60 с (§5)"
    assert session.consecutive_404 == 0  # серия прервана после успеха
    assert calls[-1].proxy_url != calls[2].proxy_url  # IP сменён (§3.2)


async def test_direct_mode_works_without_proxy():
    _, sleeper = make_recorder_sleeper()
    session = AntiBanSession(ProxyRotator(), rng=random.Random(1), sleeper=sleeper)
    calls, fetch = make_fetcher(
        [FetchResponse(200), FetchResponse(200, url=VACANCY_URL)]
    )
    response = await session.fetch(VACANCY_URL, fetcher=fetch)
    assert response.status_code == 200
    assert all(call.proxy_url is None for call in calls)  # direct dev-режим




