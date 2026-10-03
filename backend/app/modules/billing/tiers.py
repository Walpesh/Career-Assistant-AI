"""Billing Module — тарифные планы и суточные квоты (TASK «Monetization Engine»).

Каталог тарифов — единственный источник правды о лимитах: код нигде не
содержит «магических» чисел вроде ``if daily > 10``. Любой тариф описывается
одной записью :class:`TierQuota`, поэтому добавление нового плана — это
одна строка в :data:`TIERS`, а не правка в роутерах.

Квоты считаются по календарным суткам (UTC) и по видам операций
(:class:`QuotaKind`):

======================  =====================================================
``QuotaKind``           Что ограничивает
======================  =====================================================
``parse``               количество задач парсинга (авто/группа/ручной)
``letter``              количество сгенерированных сопроводительных писем
``analysis``            количество анализов вакансий
``proxy_mb``            объём прокси-трафика, МБ (Proxy Usage Logger)
======================  =====================================================

Значение ``UNLIMITED`` (-1) означает «без ограничения»: такие тарифы
предназначены для enterprise-контрактов и не должны блокировать работу.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "FREE",
    "PRO",
    "ENTERPRISE",
    "TIERS",
    "QuotaKind",
    "QuotaState",
    "TierQuota",
    "UNLIMITED",
    "available_tiers",
    "quota_for",
    "resolve_tier",
]


class QuotaKind:
    """Виды расходуемых квот (значения — CHECK в `usage_counters`)."""

    PARSE = "parse"
    LETTER = "letter"
    ANALYSIS = "analysis"
    PROXY_MB = "proxy_mb"

    ALL: tuple[str, ...] = (PARSE, LETTER, ANALYSIS, PROXY_MB)


#: Смысловое значение «без ограничения» для числовых квот.
UNLIMITED = -1


@dataclass(frozen=True)
class TierQuota:
    """Суточные лимиты одного тарифа.

    Все лимиты — «в сутки» и «на пользователя»; обнуление происходит по
    календарным суткам UTC (см. Billing Module ``quota_day()``).
    """

    #: Максимум задач парсинга в сутки.
    parse: int
    #: Максимум сгенерированных писем в сутки.
    letter: int
    #: Максимум анализов в сутки.
    analysis: int
    #: Максимум прокси-трафика в сутки, МБ.
    proxy_mb: int
    #: Приоритет тарифа: выше — приоритетнее (используется для сортировки).
    rank: int = 0

    def limit_for(self, kind: str) -> int:
        """Лимит по виду квоты; неизвестный вид → без ограничения.

        Неизвестный вид не должен «закрывать» пользователю операцию: лучше
        пропустить проверку, чем отдать 429 QUOTA_EXCEEDED на валидном виде,
        который просто ещё не добавлен в каталог.
        """
        value = getattr(self, kind, UNLIMITED)
        return UNLIMITED if value is None else int(value)

    def is_unlimited(self, kind: str) -> bool:
        """True — квота этого вида не ограничена."""
        return self.limit_for(kind) == UNLIMITED

    def as_dict(self) -> dict[str, int]:
        """Квоты в виде словаря (для JSON-ответа /billing/usage)."""
        return {kind: self.limit_for(kind) for kind in QuotaKind.ALL}


#: Тарифы продукта. Порядок — от младшего к старшему (``rank`` растёт).
FREE = TierQuota(parse=5, letter=10, analysis=30, proxy_mb=200, rank=0)
PRO = TierQuota(parse=50, letter=200, analysis=1000, proxy_mb=5000, rank=1)
ENTERPRISE = TierQuota(
    parse=UNLIMITED, letter=UNLIMITED, analysis=UNLIMITED, proxy_mb=UNLIMITED, rank=2
)

#: Каталог тарифов по имени.
TIERS: dict[str, TierQuota] = {
    "free": FREE,
    "pro": PRO,
    "enterprise": ENTERPRISE,
}

#: Тариф по умолчанию — для аккаунта без строки `subscriptions`.
DEFAULT_TIER = "free"


def available_tiers() -> list[dict]:
    """Каталог тарифов для публичного эндпоинта (docs/03 §11)."""
    return [
        {
            "tier": name,
            "rank": quota.rank,
            "daily_quotas": quota.as_dict(),
            "unlimited": [kind for kind in QuotaKind.ALL if quota.is_unlimited(kind)],
        }
        for name, quota in sorted(TIERS.items(), key=lambda item: item[1].rank)
    ]


def quota_for(tier: str | None) -> TierQuota:
    """Квоты тарифа; неизвестное имя откатывается к free (fail-safe).

    Откат к ``free``, а не к ``UNLIMITED`` — сознательно: неизвестный тариф
    из базы не должен молча снять лимиты.
    """
    return TIERS.get((tier or DEFAULT_TIER).strip().lower(), FREE)


def resolve_tier(tier: str | None) -> str:
    """Нормализовать имя тарифа к одному из ``free / pro / enterprise``."""
    normalized = (tier or DEFAULT_TIER).strip().lower()
    return normalized if normalized in TIERS else DEFAULT_TIER


@dataclass(frozen=True)
class QuotaState:
    """Состояние одной квоты пользователя на текущие сутки."""

    kind: str
    used: int
    limit: int
    #: До какого момента счётчик сбрасывается (следующие сутки UTC).
    resets_at: str

    @property
    def unlimited(self) -> bool:
        return self.limit == UNLIMITED

    @property
    def remaining(self) -> int:
        """Остаток квоты; для безлимита — -1 (совпадает с маркером)."""
        if self.unlimited:
            return UNLIMITED
        return max(0, self.limit - self.used)

    @property
    def exhausted(self) -> bool:
        """True — квота исчерпана и операцию запускать нельзя."""
        return not self.unlimited and self.used >= self.limit
