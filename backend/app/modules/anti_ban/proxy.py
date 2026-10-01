"""Резидентные прокси и ротатор sticky-сессий (docs/04_PARSING_RULES.md §3.1–§3.2).

docs/04 §3.1: только резидентные (Residential) прокси, датацентровые запрещены,
желательно российские подсети (или страны пользователя).
docs/04 §3.2 (политика ротации): sticky-сессия 5–10 минут на один IP; или
принудительная смена после 60–80 запросов; при 429 / капче / серии 404 —
немедленная смена IP + пауза.

Источники endpoints (см. backend/.env.example и app.core.config):
    - PROXY_LIST    — явные URL через запятую (несколько exit'ов, round-robin);
    - PROXY_GATEWAY — шлюз провайдера host:port; sticky-сессия реализуется
                      суффиксом `-session-<id>` в username (конвенция Bright
                      Data / Smartproxy / IProyal), смена сессии = смена IP;
    - ничего не задано — direct-режим (dev): URL прокси None.
"""

from __future__ import annotations

import random
import secrets
import time
from dataclasses import dataclass
from urllib.parse import quote, urlsplit

from app.core.config import Settings, get_settings
from app.modules.anti_ban.constants import (
    REQUESTS_PER_IP_MAX,
    REQUESTS_PER_IP_MIN,
    STICKY_SESSION_MAX_SECONDS,
    STICKY_SESSION_MIN_SECONDS,
)
from app.modules.anti_ban.exceptions import ProxyConfigError, ProxyExhaustedError

__all__ = [
    "RESIDENTIAL_PROVIDERS",
    "ProxyEndpoint",
    "StickySession",
    "ProxyRotator",
    "build_endpoints",
    "validate_residential",
]

#: Резидентные провайдеры (docs/04 §3.1: датацентровые запрещены).
RESIDENTIAL_PROVIDERS: frozenset[str] = frozenset(
    "brightdata decodo smartproxy iproyal soax proxy-seller "
    "infatica oxylabs netnut proxyempire".split()
)

SUPPORTED_SCHEMES = frozenset({"http", "https", "socks5", "socks5h"})
_SESSION_PLACEHOLDER = "{session}"


def _normalize_provider(provider: str) -> str:
    return provider.strip().lower().replace("_", "").replace(" ", "")


def validate_residential(provider: str) -> str:
    """Проверить, что провайдер резидентный (docs/04 §3.1: датацентр запрещён).

    Returns:
        Нормализованное имя провайдера.

    Raises:
        ProxyConfigError: провайдер не задан или не входит в список residential.
    """
    normalized = _normalize_provider(provider)
    if not normalized:
        raise ProxyConfigError(
            "Не задан PROXY_PROVIDER: боевой парсинг разрешён только через "
            "резидентные прокси (docs/04 §3.1)"
        )
    if normalized not in RESIDENTIAL_PROVIDERS:
        raise ProxyConfigError(
            f"Провайдер «{provider}» не входит в список резидентных "
            f"({', '.join(sorted(RESIDENTIAL_PROVIDERS))}). Датацентровые "
            "прокси запрещены (docs/04 §3.1)"
        )
    return normalized


@dataclass(frozen=True)
class ProxyEndpoint:
    """Один exit-адрес; url шаблона содержит {session} для sticky-ротации."""

    url: str | None  # None → direct (прокси не настроен, dev-режим)
    provider: str = ""
    template: bool = False  # url содержит {session} (gateway-режим)

    @property
    def label(self) -> str:
        """Метка без пароля — для логов, cooldown и dead-множества."""
        if not self.url:
            return "direct"
        parts = urlsplit(self.url)
        auth = f"{parts.username}@" if parts.username else ""
        port = f":{parts.port}" if parts.port else ""
        return f"{parts.scheme}://{auth}{parts.hostname}{port}"

    def with_session(self, token: str) -> "ProxyEndpoint":
        """Новая sticky-сессия: токен подставляется в username шаблона."""
        if not self.template or self.url is None:
            return self
        return ProxyEndpoint(
            url=self.url.replace(_SESSION_PLACEHOLDER, token),
            provider=self.provider,
            template=False,
        )

    def __repr__(self) -> str:  # пароль никогда не попадает в логи
        return f"ProxyEndpoint(label={self.label!r}, provider={self.provider!r})"


def _parse_proxy_url(raw: str, provider: str) -> ProxyEndpoint:
    parts = urlsplit(raw)
    if parts.scheme not in SUPPORTED_SCHEMES or not parts.hostname:
        raise ProxyConfigError(
            f"Некорректный URL прокси «{raw}» "
            "(ожидается scheme://[user:pass@]host:port)"
        )
    if parts.port is None:
        raise ProxyConfigError(f"В прокси не указан порт: «{raw}»")
    return ProxyEndpoint(url=raw, provider=provider, template=False)


def build_endpoints(settings: Settings | None = None) -> tuple[ProxyEndpoint, ...]:
    """Собрать endpoints из настроек приложения (docs/04 §3.1).

    Пустая конфигурация → () — direct-режим (dev); боевой режим обязан
    указывать резидентный провайдер (проверка validate_residential).
    """
    cfg = settings if settings is not None else get_settings()
    entries = [e.strip() for e in cfg.proxy_list.split(",") if e.strip()]
    gateway = cfg.proxy_gateway.strip()
    has_gateway = bool(gateway and cfg.proxy_username)

    if not entries and not has_gateway:
        return ()  # direct-режим: прокси не настроен

    normalized = validate_residential(cfg.proxy_provider.strip())

    if entries:  # явный список exit'ов → round-robin ротация
        return tuple(_parse_proxy_url(raw, normalized) for raw in entries)

    # Gateway-режим: одна точка входа, ротация = смена session-токена в username.
    scheme = "http"
    if "://" in gateway:
        scheme, gateway = gateway.split("://", 1)
    if scheme not in SUPPORTED_SCHEMES or ":" not in gateway:
        raise ProxyConfigError(
            f"PROXY_GATEWAY должен быть host:port — получено «{cfg.proxy_gateway}»"
        )
    session_user = quote(str(cfg.proxy_username), safe="")
    if _SESSION_PLACEHOLDER not in session_user:
        session_user = f"{session_user}-session-{_SESSION_PLACEHOLDER}"
    password = quote(str(cfg.proxy_password), safe="")
    template_url = f"{scheme}://{session_user}:{password}@{gateway}"
    return (ProxyEndpoint(url=template_url, provider=normalized, template=True),)


@dataclass
class StickySession:
    """Sticky-сессия одного IP (docs/04 §3.2: 5–10 мин ИЛИ 60–80 запросов)."""

    endpoint_label: str
    started_at: float
    lifetime_seconds: float  # randint(300, 600) — §3.2: 5–10 минут
    max_requests: int  # randint(60, 80) — §1/§3.2: 60–80 запросов
    requests: int = 0

    def record(self) -> None:
        """Засчитать выполненный запрос к текущему IP."""
        self.requests += 1

    def expired(self, now: float) -> bool:
        """Сессия исчерпала лимит времени или запросов → нужна ротация."""
        return (
            now - self.started_at >= self.lifetime_seconds
            or self.requests >= self.max_requests
        )

    def reason(self, now: float) -> str:
        """Причина ротации: «requests» (60–80) или «lifetime» (5–10 мин)."""
        if self.requests >= self.max_requests:
            return "requests"
        return "lifetime"


class ProxyRotator:
    """Ротатор sticky-сессий: время/счётчик, failover и cooldown (§3.2).

    - acquire() — получить текущий IP (ротирует сам при исчерпании лимитов);
    - rotate()  — перейти на следующий доступный IP;
    - report_failure(endpoint) — прокси мёртв → failover на новый IP;
    - report_rate_limit(pause) — 429/капча/серия 404 → cooldown + смена IP;
    - revive()  — вернуть endpoint в пул после лечения проблемы.

    Direct (dev) режим без endpoints: методы возвращают None.
    """

    def __init__(
        self,
        endpoints: tuple[ProxyEndpoint, ...] | list[ProxyEndpoint] = (),
        *,
        rng: random.Random | None = None,
        clock=time.monotonic,
        lifetime_range: tuple[float, float] = (
            STICKY_SESSION_MIN_SECONDS,
            STICKY_SESSION_MAX_SECONDS,
        ),
        request_range: tuple[int, int] = (REQUESTS_PER_IP_MIN, REQUESTS_PER_IP_MAX),
    ) -> None:
        self._endpoints = list(endpoints)
        self._rng = rng if rng is not None else random
        self._clock = clock
        self._lifetime_range = lifetime_range
        self._request_range = request_range
        self._index = -1
        self._current: ProxyEndpoint | None = None
        self._session: StickySession | None = None
        self._cooldowns: dict[str, float] = {}  # label → monotonic-момент готовности
        self._dead: set[str] = set()  # labels мёртвых (failover)
        self.rotations = 0  # статистика для мониторинга (docs/04 §9)
        self.last_reason: str | None = None

    @classmethod
    def from_settings(cls, settings: Settings | None = None, **kwargs) -> "ProxyRotator":
        """Ротатор из настроек приложения (.env)."""
        return cls(build_endpoints(settings), **kwargs)

    # --- состояние -----------------------------------------------------------
    @property
    def endpoints(self) -> tuple[ProxyEndpoint, ...]:
        return tuple(self._endpoints)

    @property
    def session(self) -> StickySession | None:
        return self._session

    @property
    def current(self) -> ProxyEndpoint | None:
        return self._current

    @property
    def current_url(self) -> str | None:
        return self._current.url if self._current else None

    def should_rotate(self) -> bool:
        """Проверить лимиты docs/04 §3.2 (время жизни или число запросов)."""
        if self._current is None or self._session is None:
            return False
        return self._session.expired(self._clock())

    def record_request(self) -> None:
        """Увеличить счётчик запросов текущей sticky-сессии (§3.2)."""
        if self._session is not None:
            self._session.record()

    # --- выбор IP ------------------------------------------------------------
    def acquire(self) -> ProxyEndpoint | None:
        """Текущий IP; ротирует при исчерпании лимитов sticky-сессии (§3.2)."""
        if not self._endpoints:
            return None  # direct-режим (dev)
        if self._current is None:
            return self.rotate("first")
        if self.should_rotate():
            assert self._session is not None
            return self.rotate(self._session.reason(self._clock()))
        return self._current

    def rotate(self, reason: str = "forced") -> ProxyEndpoint | None:
        """Перейти на следующий доступный IP (пропуская dead/cooldown)."""
        if not self._endpoints:
            return None
        now = self._clock()
        total = len(self._endpoints)
        for offset in range(1, total + 1):
            idx = (self._index + offset) % total
            endpoint = self._expand(self._endpoints[idx])
            if endpoint.label in self._dead:
                continue
            if self._cooldowns.get(endpoint.label, 0.0) > now:
                continue
            self._adopt(idx, endpoint, now, reason)
            return endpoint
        return self._rotate_among_cooling(now, reason)

    def _rotate_among_cooling(self, now: float, reason: str) -> ProxyEndpoint | None:
        """Все остывают → ближайший по cooldown; все мёртвы → исчерпаны (§3.2)."""
        cooling: list[tuple[float, int, ProxyEndpoint]] = []
        for idx, base in enumerate(self._endpoints):
            endpoint = self._expand(base)
            if endpoint.label in self._dead:
                continue
            cooling.append((self._cooldowns.get(endpoint.label, 0.0), idx, endpoint))
        if not cooling:
            raise ProxyExhaustedError(
                f"Все {len(self._endpoints)} прокси помечены мёртвыми — "
                "failover исчерпан (вызовите revive() или добавьте endpoints)"
            )
        cooling.sort(key=lambda item: item[0])  # сначала ближайший к готовности
        _, idx, endpoint = cooling[0]
        self._adopt(idx, endpoint, now, reason)
        return endpoint

    def _expand(self, endpoint: ProxyEndpoint) -> ProxyEndpoint:
        """Gateway-шаблон → конкретная sticky-сессия (новый session-токен)."""
        if endpoint.template:
            return endpoint.with_session(secrets.token_hex(3))
        return endpoint

    def _adopt(self, idx: int, endpoint: ProxyEndpoint, now: float, reason: str) -> None:
        """Зафиксировать новый IP и открыть новую sticky-сессию (§3.2)."""
        self._index = idx
        self._current = endpoint
        self._session = StickySession(
            endpoint_label=endpoint.label,
            started_at=now,
            lifetime_seconds=float(
                self._rng.randint(
                    int(self._lifetime_range[0]), int(self._lifetime_range[1])
                )
            ),
            max_requests=int(
                self._rng.randint(
                    int(self._request_range[0]), int(self._request_range[1])
                )
            ),
        )
        self.rotations += 1
        self.last_reason = reason

    # --- реакция на защиту (docs/04 §3.2, §5) --------------------------------
    def report_failure(self, endpoint: ProxyEndpoint | None = None) -> ProxyEndpoint | None:
        """Прокси мёртв → пометить и немедленно сделать failover на новый IP."""
        target = endpoint if endpoint is not None else self._current
        if target is not None and target.label:
            self._dead.add(target.label)
        return self.rotate("failover")

    def report_rate_limit(self, pause_seconds: float = 0.0) -> ProxyEndpoint | None:
        """429/капча/серия 404 → cooldown старого IP + немедленная смена (§3.2)."""
        if self._current is not None and pause_seconds > 0:
            self._cooldowns[self._current.label] = self._clock() + pause_seconds
        return self.rotate("rate_limit")

    def revive(self, endpoint: ProxyEndpoint | None = None) -> None:
        """Вернуть endpoint (или все) в пул после лечения проблемы."""
        if endpoint is None:
            self._dead.clear()
            self._cooldowns.clear()
        else:
            self._dead.discard(endpoint.label)
            self._cooldowns.pop(endpoint.label, None)



