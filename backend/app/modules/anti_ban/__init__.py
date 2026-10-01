"""Proxy & Anti-Ban Module — прокси, fingerprints, обход защит hh.ru (docs/01 §3).

Публичный интерфейс (параметры — docs/04_PARSING_RULES.md §1, §3, §5, §9):
    ProxyRotator, StickySession, build_endpoints — ротация резидентных прокси
        (sticky 5–10 мин или 60–80 запросов; failover; cooldown);
    Fingerprint, generate_fingerprint, generate_user_agent — User-Agent +
        согласованный полный набор браузерных заголовков;
    next_delay, human_sleep — задержки 4–8 с (нормальное распределение);
    AntiBanSession, PreparedRequest, FetchResponse, warmup_steps — прогрев на
        https://hh.ru/, cookies, retry-цикл «успешный HTML или ошибка»;
    classify_response, detect_rate_limit, detect_captcha, detect_throttling,
        backoff_delay — детект 429 / Cloudflare / hh-капчи / серии 404;
    cubic_bezier_points, random_mouse_path, scroll_deltas, reading_pause —
        human-mimicry для Playwright.
"""

from app.modules.anti_ban.constants import WARMUP_URL  # noqa: F401
from app.modules.anti_ban.delays import human_sleep, next_delay
from app.modules.anti_ban.detectors import (
    Detection,
    ThreatKind,
    backoff_delay,
    captcha_rate_exceeded,
    classify_response,
    detect_captcha,
    detect_cloudflare_challenge,
    detect_rate_limit,
    detect_throttling,
)
from app.modules.anti_ban.exceptions import (
    AntiBanError,
    CaptchaDetected,
    ProxyConfigError,
    ProxyError,
    ProxyExhaustedError,
    RateLimitExceeded,
)
from app.modules.anti_ban.fingerprint import (
    Fingerprint,
    generate_fingerprint,
    generate_user_agent,
)
from app.modules.anti_ban.human import (
    cubic_bezier_points,
    random_mouse_path,
    reading_pause,
    scroll_deltas,
)
from app.modules.anti_ban.proxy import (
    ProxyEndpoint,
    ProxyRotator,
    StickySession,
    build_endpoints,
    validate_residential,
)
from app.modules.anti_ban.session import (
    AntiBanSession,
    FetchResponse,
    PreparedRequest,
    warmup_steps,
)
from app.modules.anti_ban.router import router  # noqa: F401

__all__ = [
    "AntiBanError",
    "AntiBanSession",
    "Detection",
    "FetchResponse",
    "Fingerprint",
    "PreparedRequest",
    "ProxyConfigError",
    "ProxyEndpoint",
    "ProxyError",
    "ProxyExhaustedError",
    "ProxyRotator",
    "RateLimitExceeded",
    "StickySession",
    "ThreatKind",
    "WARMUP_URL",
    "backoff_delay",
    "build_endpoints",
    "captcha_rate_exceeded",
    "classify_response",
    "cubic_bezier_points",
    "detect_captcha",
    "detect_cloudflare_challenge",
    "detect_rate_limit",
    "detect_throttling",
    "generate_fingerprint",
    "generate_user_agent",
    "human_sleep",
    "next_delay",
    "random_mouse_path",
    "reading_pause",
    "router",
    "scroll_deltas",
    "validate_residential",
    "warmup_steps",
]

