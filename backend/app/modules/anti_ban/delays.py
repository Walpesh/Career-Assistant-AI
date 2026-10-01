"""Генератор случайных задержек между запросами (docs/04_PARSING_RULES.md §1).

«Случайная задержка 4–8 секунд между запросами (лучше нормальное распределение)» —
используется нормальное (гауссово) распределение μ=6 с, σ=1 с с обрезкой до [4, 8],
что даёт пик в середине интервала и редкие «длинные» паузы у границ.
"""

from __future__ import annotations

import asyncio
import random

from app.modules.anti_ban.constants import (
    DELAY_MAX_SECONDS,
    DELAY_MEAN_SECONDS,
    DELAY_MIN_SECONDS,
    DELAY_SIGMA_SECONDS,
)

__all__ = ["next_delay", "human_sleep"]


def next_delay(
    rng: random.Random | None = None,
    *,
    min_seconds: float = DELAY_MIN_SECONDS,
    max_seconds: float = DELAY_MAX_SECONDS,
    mean: float = DELAY_MEAN_SECONDS,
    sigma: float = DELAY_SIGMA_SECONDS,
) -> float:
    """Следующая пауза между запросами, сек (нормальное распределение, 4–8 с).

    Args:
        rng: источник случайности (модуль random по умолчанию; в тестах — Random(seed)).
        min_seconds/max_seconds: границы (по умолчанию лимиты docs/04 §1).
        mean/sigma: параметры нормального распределения.
    """
    source = rng if rng is not None else random
    value = source.gauss(mean, sigma)
    clamped = min(max(value, min_seconds), max_seconds)
    return round(clamped, 3)


async def human_sleep(
    delay: float | None = None,
    *,
    rng: random.Random | None = None,
    sleeper=None,
) -> float:
    """«Человеческая» пауза перед запросом (docs/04 §1: 4–8 с).

    Args:
        delay: явная задержка (для вызывающего кода/тестов); clamp'ится в [4, 8].
        rng: источник случайности для next_delay.
        sleeper: асинхронная функция ожидания (по умолчанию asyncio.sleep);
                 подменяется в тестах, чтобы не ждать реальное время.

    Returns:
        Фактическое время ожидания в секундах.
    """
    seconds = (
        round(min(max(delay, DELAY_MIN_SECONDS), DELAY_MAX_SECONDS), 3)
        if delay is not None
        else next_delay(rng)
    )
    sleep = sleeper if sleeper is not None else asyncio.sleep
    await sleep(seconds)
    return seconds
