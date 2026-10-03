"""Сессия обхода защит: прогрев, cookies, ротация IP, retry-цикл.

Контракт модуля (docs/01 §3): «Запрос на HTTP/браузер → Успешный HTML/JSON
или ошибка»; параметры — docs/04_PARSING_RULES.md §1, §3.2–§3.3, §5.

Порядок работы сессии:
    1. warm_up() — «первый запрос всегда на https://hh.ru/» (§3.3);
    2. prepare() — ротация IP по лимитам §3.2 + отпечаток + cookies;
    3. перед каждым запросом — human-пауза 4–8 с, нормальное распределение (§1);
    4. handle_response() — детект 429 / Cloudflare-капчи / серии 404 (§5);
    5. 429 → смена IP + exponential backoff; капча → CaptchaDetected
       (waiting_captcha); серия 404 → пауза 30–60 с + смена IP;
    6. сетевая ошибка прокси → failover на следующий IP (§3.2).

fetcher/sleeper внедряются вызывающим кодом — сессия тестируется без реальных
запросов и без реального ожидания.
"""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass, field
from http.cookies import SimpleCookie
from typing import Awaitable, Callable, Mapping

from app.modules.anti_ban.constants import (
    DETECTION_BODY_LIMIT,
    MAX_FETCH_ATTEMPTS,
    THROTTLING_PAUSE_MAX_SECONDS,
    THROTTLING_PAUSE_MIN_SECONDS,
    WARMUP_URL,
)
from app.modules.anti_ban.delays import next_delay
from app.modules.anti_ban.detectors import (
    Detection,
    ThreatKind,
    backoff_delay,
    captcha_rate_exceeded as check_captcha_rate,
    classify_response,
)
from app.modules.anti_ban.exceptions import (
    AntiBanError,
    CaptchaDetected,
    ProxyError,
    RateLimitExceeded,
)
from app.core.logging import get_logger
from app.modules.anti_ban.fingerprint import Fingerprint, generate_fingerprint
from app.modules.anti_ban.proxy import ProxyEndpoint, ProxyRotator
from app.modules.metrics.registry import inc_captcha, inc_fetch_total

__all__ = ["FetchResponse", "PreparedRequest", "AntiBanSession", "warmup_steps"]

log = get_logger(__name__)


def warmup_steps() -> list[str]:
    """План прогрева сессии (docs/04 §3.3): первый запрос всегда на homepage."""
    return [WARMUP_URL]


@dataclass
class FetchResponse:
    """Минимальный адаптер ответа (httpx.Response или фейк в тестах)."""

    status_code: int
    headers: Mapping[str, str] = field(default_factory=dict)
    text: str = ""
    url: str = ""

    @classmethod
    def from_httpx(cls, response) -> "FetchResponse":
        """Обернуть реальный httpx-ответ."""
        return cls(
            status_code=response.status_code,
            headers=response.headers,
            text=response.text,
            url=str(response.url),
        )


@dataclass
class PreparedRequest:
    """Подготовленный запрос: прокси + отпечаток + cookies одной сессии."""

    url: str
    headers: dict[str, str]
    cookies: dict[str, str]
    proxy_url: str | None  # None → direct (dev-режим без прокси)
    endpoint: ProxyEndpoint | None
    fingerprint: Fingerprint


#: Тип fetcher'а: получает PreparedRequest → отдаёт объект вида FetchResponse.
Fetcher = Callable[[PreparedRequest], Awaitable[FetchResponse]]


class AntiBanSession:
    """Сессия обхода защит hh.ru: прогрев, ротация IP, cookies, retry (§3–§5)."""

    def __init__(
        self,
        rotator: ProxyRotator | None = None,
        *,
        rng: random.Random | None = None,
        sleeper: Callable[[float], Awaitable[None]] | None = None,
        fetcher: Fetcher | None = None,
        fingerprint: Fingerprint | None = None,
    ) -> None:
        self.rotator = rotator if rotator is not None else ProxyRotator.from_settings()
        self.rng = rng if rng is not None else random
        self._sleeper = sleeper if sleeper is not None else asyncio.sleep
        self._fetcher = fetcher
        self.fingerprint = (
            fingerprint if fingerprint is not None else generate_fingerprint(self.rng)
        )
        self.cookies: dict[str, str] = {}  # переиспользуются в рамках сессии (§3.3)
        self.warmed_up = False
        self.consecutive_404 = 0  # счётчик серии 404 (троттлинг, §5)
        self.request_count = 0  # отправлено запросов (включая прогрев/ретраи)
        self.response_count = 0  # получено ответов
        self.captcha_count = 0  # срабатываний капчи (доля для docs/04 §9)

    # --- статистика (docs/04 §9) ---------------------------------------------
    @property
    def captcha_rate(self) -> float:
        """Доля ответов с капчью."""
        if self.response_count == 0:
            return 0.0
        return self.captcha_count / self.response_count

    @property
    def captcha_rate_exceeded(self) -> bool:
        """Доля капчи > 5% → снижать интенсивность парсинга (docs/04 §9)."""
        return check_captcha_rate(self.response_count, self.captcha_count)

    # --- подготовка запроса ---------------------------------------------------
    def prepare(self, url: str, *, dest: str = "document") -> PreparedRequest:
        """Собрать запрос: IP по лимитам §3.2 + отпечаток + cookies сессии."""
        endpoint = self.rotator.acquire()
        headers = self.fingerprint.headers(dest=dest)
        if self.cookies:
            headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
        self.rotator.record_request()
        self.request_count += 1
        return PreparedRequest(
            url=url,
            headers=headers,
            cookies=dict(self.cookies),
            proxy_url=endpoint.url if endpoint else None,
            endpoint=endpoint,
            fingerprint=self.fingerprint,
        )

    def _store_cookies(self, response) -> None:
        """Сохранить Set-Cookie ответа: cookies живут внутри сессии (§3.3)."""
        raw_cookies = getattr(response, "cookies", None)
        if raw_cookies:
            self.cookies.update(dict(raw_cookies))
            return
        headers = getattr(response, "headers", None) or {}
        if hasattr(headers, "get_list"):
            values = headers.get_list("set-cookie")
        else:
            single = headers.get("set-cookie") or headers.get("Set-Cookie")
            values = [single] if single else []
        for value in values:
            jar = SimpleCookie()
            jar.load(str(value))
            for name, morsel in jar.items():
                self.cookies[name] = morsel.value

    def handle_response(self, response, *, url: str = "") -> Detection:
        """Классифицировать ответ детекторами и обновить счётчики сессии.

        Не бросает исключений — реакцию (backoff/смена IP/остановка)
        выполняет fetch(); вызывается напрямую в тестах и анализаторах.
        """
        headers = getattr(response, "headers", {}) or {}
        text = (getattr(response, "text", "") or "")[:DETECTION_BODY_LIMIT]
        detection = classify_response(
            getattr(response, "status_code", 0),
            headers=headers,
            body=text,
            url=url or (getattr(response, "url", "") or ""),
            consecutive_404=self.consecutive_404,
        )
        self.response_count += 1
        inc_fetch_total(detection.kind.value)
        if detection.kind is ThreatKind.CAPTCHA:
            self.captcha_count += 1
            inc_captcha(detection.detail or "captcha")
            log.warning(
                "captcha_detected",
                url=url,
                detail=detection.detail,
                captcha_count=self.captcha_count,
                response_count=self.response_count,
            )
        if detection.kind in (ThreatKind.NOT_FOUND, ThreatKind.THROTTLING):
            self.consecutive_404 += 1  # серия404 растёт (§5)
        elif detection.kind is ThreatKind.OK:
            self.consecutive_404 = 0  # успешный ответ прерывает серию
        return detection

    # --- прогрев (docs/04 §3.3) ----------------------------------------------
    async def warm_up(self, fetcher: Fetcher | None = None):
        """Прогрев: первый запрос всегда на https://hh.ru/ (§3.3).

        Cookies homepage сохраняются и переиспользуются на целевых страницах.
        Сетевая ошибка прокси → failover на новый IP и повтор.

        Raises:
            ProxyError: прокси недоступны все MAX_FETCH_ATTEMPTS попыток.
            CaptchaDetected: капча уже на прогреве (waiting_captcha, §5).
        """
        fetch = fetcher if fetcher is not None else self._fetcher
        if fetch is None:
            raise AntiBanError("Для прогрева сессии нужен fetcher")
        last_error: Exception | None = None
        for _ in range(MAX_FETCH_ATTEMPTS):
            request = self.prepare(WARMUP_URL)
            try:
                response = await fetch(request)
            except ProxyError as exc:
                self.rotator.report_failure(request.endpoint)  # failover (§3.2)
                last_error = exc
                continue
            self._store_cookies(response)
            detection = self.handle_response(response, url=WARMUP_URL)
            if detection.kind is ThreatKind.CAPTCHA:
                raise CaptchaDetected(detection.detail)
            if detection.kind is ThreatKind.RATE_LIMITED:
                delay = backoff_delay(1, detection.retry_after, self.rng)
                self.rotator.report_rate_limit(delay)
                await self._wait(delay)
                continue
            self.warmed_up = True  # homepage получен → сессия прогрета
            return response
        raise ProxyError(
            f"Прогрев сессии не удался за {MAX_FETCH_ATTEMPTS} попыток: {last_error}"
        )

    # --- основной запрос (docs/04 §5) ----------------------------------------
    async def fetch(
        self,
        url: str,
        *,
        fetcher: Fetcher | None = None,
        dest: str = "document",
        max_attempts: int = MAX_FETCH_ATTEMPTS,
    ):
        """Получить страницу: прогрев → human-пауза → запрос → реакция на защиту.

        Returns:
            Ответ 200 (или единичный 404 — его дальше разбирает вызывающий код).

        Raises:
            CaptchaDetected   — капча: остановка воркера (waiting_captcha, §5);
            RateLimitExceeded — 429/троттлинг не отступил за попытки (retry, §5);
            ProxyError        — failover исчерпан, живых прокси нет.
        """
        fetch = fetcher if fetcher is not None else self._fetcher
        if fetch is None:
            raise AntiBanError("Для запроса нужен fetcher")
        if not self.warmed_up:
            await self.warm_up(fetch)

        waited = False  # human-пауза 4–8 с уже сделана на этом шаге?
        last_detection: Detection | None = None
        last_error: Exception | None = None

        for attempt in range(1, max_attempts + 1):
            if not waited:
                await self._wait(next_delay(self.rng))  # §1: 4–8 с между запросами
            waited = False

            request = self.prepare(url, dest=dest)
            try:
                response = await fetch(request)
            except ProxyError as exc:
                last_error, last_detection = exc, None
                self.rotator.report_failure(request.endpoint)  # failover (§3.2)
                continue

            last_error = None
            self._store_cookies(response)
            last_detection = self.handle_response(response, url=url)
            detection = last_detection

            if detection.kind in (ThreatKind.OK, ThreatKind.NOT_FOUND):
                return response
            if detection.kind is ThreatKind.CAPTCHA:
                raise CaptchaDetected(detection.detail)
            if detection.kind is ThreatKind.RATE_LIMITED:
                delay = backoff_delay(attempt, detection.retry_after, self.rng)
                self.rotator.report_rate_limit(delay)  # смена IP + пауза (§3.2)
                await self._wait(delay)
                waited = True
                continue
            if detection.kind is ThreatKind.THROTTLING:
                delay = self.rng.uniform(
                    THROTTLING_PAUSE_MIN_SECONDS, THROTTLING_PAUSE_MAX_SECONDS
                )
                self.consecutive_404 = 0  # серия прервана после митигации
                self.rotator.report_rate_limit(delay)  # пауза 30–60 + IP (§5)
                await self._wait(delay)
                waited = True
                continue
            # SERVER_ERROR/нестандартный статус — повтор с backoff, без смены IP.
            await self._wait(backoff_delay(attempt, None, self.rng))
            waited = True

        if last_detection is None and last_error is not None:
            raise ProxyError(
                f"Запрос {url} не удался за {max_attempts} попыток: {last_error}"
            )
        detail = last_detection.detail if last_detection else "неизвестная ошибка"
        raise RateLimitExceeded(
            f"Запрос {url} не удался за {max_attempts} попыток: {detail}"
        )

    async def _wait(self, seconds: float) -> None:
        """Ожидание через внедрённый sleeper (тесты не ждут реальное время)."""
        await self._sleeper(round(float(seconds), 3))


