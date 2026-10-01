"""Детекторы защит hh.ru / Cloudflare (docs/04_PARSING_RULES.md §3.2, §5, §9).

Реакция на срабатывания — строго по таблице docs/04 §5:
    HTTP 429             → смена прокси + exponential backoff (retry);
    Капча                → смена IP + остановка воркера (waiting_captcha);
    Серия 404            → троттлинг: пауза 30–60 с + смена IP (retry);
    Удалённая вакансия   → единичный 404 (not_found → error у вызывающего кода).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from enum import Enum

from app.modules.anti_ban.constants import (
    BACKOFF_BASE_SECONDS,
    BACKOFF_CAP_SECONDS,
    CAPTCHA_RATE_THRESHOLD,
    CONSECUTIVE_404_THRESHOLD,
    THROTTLING_PAUSE_MAX_SECONDS,
    THROTTLING_PAUSE_MIN_SECONDS,
)

__all__ = [
    "ThreatKind",
    "Detection",
    "parse_retry_after",
    "detect_rate_limit",
    "detect_captcha",
    "detect_cloudflare_challenge",
    "detect_throttling",
    "classify_response",
    "backoff_delay",
    "captcha_rate_exceeded",
]


class ThreatKind(str, Enum):
    """Класс ответа/ситуации с точки зрения защиты."""

    OK = "ok"
    RATE_LIMITED = "rate_limited"  # HTTP 429 → смена IP + backoff
    CAPTCHA = "captcha"  # Cloudflare/hh капча → waiting_captcha
    THROTTLING = "throttling"  # серия 404 → пауза 30–60 с + смена IP
    NOT_FOUND = "not_found"  # единичный 404 → вакансия удалена
    SERVER_ERROR = "server_error"  # 5xx/нестандартный статус → повтор


@dataclass(frozen=True)
class Detection:
    """Результат классификации ответа + предписанное действие (docs/04 §5)."""

    kind: ThreatKind
    should_rotate: bool  # немедленная смена IP (§3.2: 429/капча/серия 404)
    task_status: str  # статус задачи парсинга по docs/04 §5
    detail: str = ""
    retry_after: float | None = None  # значение Retry-After, если было

    @property
    def is_ok(self) -> bool:
        return self.kind is ThreatKind.OK

    @property
    def should_retry(self) -> bool:
        return self.kind in (
            ThreatKind.RATE_LIMITED,
            ThreatKind.THROTTLING,
            ThreatKind.SERVER_ERROR,
        )


# --- маркеры тел ответов -------------------------------------------------------
_RATE_LIMIT_MARKERS = ("too many requests", "слишком много запросов", "rate-limited")

# Cloudflare interstitial («Just a moment…») — сильные маркеры, почти не
# встречаются в легитимном контенте.
_CLOUDFLARE_MARKERS = (
    "just a moment",
    "checking your browser before accessing",
    "cf-browser-verification",
    "challenge-platform",
    "__cf_chl",
    "enable javascript and cookies to continue",
    "ddos protection by cloudflare",
)

# hh.ru / Yandex SmartCaptcha — проверяются только на ошибках доступа (403/503),
# чтобы не ловить ложные срабатывания на обычной странице вакансии.
_CAPTCHA_MARKERS = (
    "smartcaptcha",
    "smart-captcha",
    "yc-softcaptcha",
    "/captcha",
    "g-recaptcha",
    "hcaptcha",
)


def _header(headers, name: str) -> str | None:
    """Заголовок без учёта регистра (dict или httpx.Headers)."""
    if not headers:
        return None
    if hasattr(headers, "get_list"):  # httpx.Headers
        values = headers.get_list(name)
        return str(values[0]) if values else None
    value = headers.get(name)
    if value is None:
        for key, val in dict(headers).items():
            if key.lower() == name.lower():
                value = val
                break
    return str(value) if value is not None else None


def parse_retry_after(value: str | None, *, now: datetime | None = None) -> float | None:
    """Retry-After → секунды (число или HTTP-дата); некорректное → None."""
    if not value:
        return None
    text = value.strip()
    try:
        return max(0.0, float(text))
    except ValueError:
        pass
    try:
        moment = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    current = now or datetime.now(timezone.utc)
    return max(0.0, (moment - current).total_seconds())


def detect_rate_limit(status_code: int, headers=None, body: str = "") -> bool:
    """HTTP 429 / признаки rate limit (docs/04 §5 → смена прокси + backoff)."""
    if status_code == 429:
        return True
    if _header(headers, "retry-after") is not None and status_code in (403, 503):
        return True  # 503 с Retry-After — типичный ответ под защитой
    lowered = body.lower()
    return status_code == 403 and any(m in lowered for m in _RATE_LIMIT_MARKERS)


def detect_cloudflare_challenge(body: str = "") -> bool:
    """Страница-челлендж Cloudflare (interstitial «Just a moment…»)."""
    lowered = body.lower()
    return any(marker in lowered for marker in _CLOUDFLARE_MARKERS)


def detect_captcha(
    status_code: int, headers=None, body: str = "", url: str = ""
) -> bool:
    """Капча Cloudflare или hh.ru (SmartCaptcha) — docs/04 §3.2, §5.

    Тело проверяется на капчу только для ошибок доступа (403/503), чтобы
    обычный HTML вакансии с упоминанием captcha-скриптов не ронял воркер.
    """
    if detect_cloudflare_challenge(body):
        return True
    if "captcha" in url.lower():  # редирект на /captcha — явный признак
        return True
    if status_code in (403, 503):
        lowered = body.lower()
        if any(marker in lowered for marker in _CAPTCHA_MARKERS):
            return True
        if "cloudflare" in lowered:
            return True
    return False


def detect_throttling(
    consecutive_not_found: int, threshold: int = CONSECUTIVE_404_THRESHOLD
) -> bool:
    """Серия 404 подряд достигла порога → троттлинг (docs/04 §5)."""
    return consecutive_not_found >= threshold


def classify_response(
    status_code: int,
    headers=None,
    body: str = "",
    url: str = "",
    consecutive_404: int = 0,
) -> Detection:
    """Классифицировать ответ и предписать действие по docs/04 §5.

    Args:
        status_code/headers/body/url: параметры ответа.
        consecutive_404: сколько 404 подряд уже было ДО этого ответа
            (серия считается сессией AntiBanSession).
    """
    retry_after = parse_retry_after(_header(headers, "retry-after"))

    if detect_rate_limit(status_code, headers, body):
        return Detection(
            ThreatKind.RATE_LIMITED,
            should_rotate=True,  # §3.2: немедленная смена IP
            task_status="retry",
            detail=f"HTTP {status_code}: превышен лимит запросов",
            retry_after=retry_after,
        )

    if detect_captcha(status_code, headers, body, url):
        return Detection(
            ThreatKind.CAPTCHA,
            should_rotate=True,  # §3.2: смена IP + остановка воркера
            task_status="waiting_captcha",
            detail="Обнаружена капча (Cloudflare/hh.ru)",
        )

    if status_code in (404, 410):
        if detect_throttling(consecutive_404 + 1):
            return Detection(
                ThreatKind.THROTTLING,
                should_rotate=True,
                task_status="retry",
                detail=(
                    f"Серия из {consecutive_404 + 1} ответов 404 подряд — "
                    f"трактуется как троттлинг: пауза "
                    f"{THROTTLING_PAUSE_MIN_SECONDS:.0f}–"
                    f"{THROTTLING_PAUSE_MAX_SECONDS:.0f} с + смена IP (docs/04 §5)"
                ),
            )
        return Detection(
            ThreatKind.NOT_FOUND,
            should_rotate=False,
            task_status="not_found",
            detail=f"HTTP {status_code}: ресурс не найден (вакансия удалена)",
        )

    if status_code != 200:
        return Detection(
            ThreatKind.SERVER_ERROR,
            should_rotate=False,
            task_status="retry",
            detail=f"HTTP {status_code}: ошибка/неожиданный статус",
        )

    return Detection(ThreatKind.OK, should_rotate=False, task_status="ok")


def backoff_delay(attempt: int, retry_after: float | None = None, rng=None) -> float:
    """Exponential backoff для HTTP 429 (docs/04 §5): 8 → 16 → 32 … до 300 с.

    База = максимуму задержки между запросами (§1); Retry-After имеет приоритет,
    если он больше расчётного значения. При переданном rng добавляется ±10%
    джиттер, чтобы воркеры не синхронизировались.
    """
    exponent = max(attempt - 1, 0)
    delay = min(BACKOFF_CAP_SECONDS, BACKOFF_BASE_SECONDS * (2**exponent))
    if retry_after is not None:
        delay = max(delay, retry_after)
    if rng is not None:
        delay *= rng.uniform(0.9, 1.1)
    return round(min(delay, max(BACKOFF_CAP_SECONDS, retry_after or 0.0)), 3)


def captcha_rate_exceeded(total: int, captcha_count: int) -> bool:
    """Доля капчи > 5% → снижать интенсивность парсинга (docs/04 §9)."""
    if total <= 0:
        return False
    return (captcha_count / total) > CAPTCHA_RATE_THRESHOLD

