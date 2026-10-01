"""Human-mimicry: параметры «живого» поведения в Playwright
(docs/04_PARSING_RULES.md §3.3).

Функции чистые и детерминированные при заданном rng — возвращают параметры
действий (точки траектории, дельты скролла, паузы), которые браузерный воркер
применяет к странице:
    - движения мыши по кривым Безье;
    - случайный скролл;
    - небольшие паузы «чтения» страницы.

Численные параметры — только из app.modules.anti_ban.constants (§3.3).
"""

from __future__ import annotations

import random

from app.modules.anti_ban.constants import (
    MOUSE_MOVE_STEPS,
    READING_PAUSE_MAX_SECONDS,
    READING_PAUSE_MIN_SECONDS,
    SCROLL_STEPS_MAX,
    SCROLL_STEPS_MIN,
    SCROLL_STEP_PX_MAX,
    SCROLL_STEP_PX_MIN,
)

__all__ = [
    "Point",
    "cubic_bezier_points",
    "random_mouse_path",
    "scroll_deltas",
    "reading_pause",
]

Point = tuple[float, float]


def cubic_bezier_points(
    start: Point,
    end: Point,
    control1: Point,
    control2: Point,
    *,
    steps: int = MOUSE_MOVE_STEPS,
) -> list[Point]:
    """Точки кубической кривой Безье от start до end (включительно).

    Первый элемент — start, последний — end: движение мыши начинается и
    заканчивается ровно в нужных координатах, как у реального пользователя.
    """
    if steps < 2:
        raise ValueError("steps должен быть не меньше 2")
    points: list[Point] = []
    for i in range(steps):
        t = i / (steps - 1)
        u = 1.0 - t
        x = (
            u**3 * start[0]
            + 3 * u**2 * t * control1[0]
            + 3 * u * t**2 * control2[0]
            + t**3 * end[0]
        )
        y = (
            u**3 * start[1]
            + 3 * u**2 * t * control1[1]
            + 3 * u * t**2 * control2[1]
            + t**3 * end[1]
        )
        points.append((x, y))
    return points


def random_mouse_path(
    start: Point,
    end: Point,
    *,
    rng: random.Random | None = None,
    steps: int = MOUSE_MOVE_STEPS,
) -> list[Point]:
    """Траектория движения мыши по Безье со случайным «дрожанием» руки.

    Управляющие точки смещаются от прямой на ±15% дистанции — траектория
    никогда не бывает идеально прямой линией (типичный признак бота).
    """
    source = rng if rng is not None else random
    dx, dy = end[0] - start[0], end[1] - start[1]

    def control(at: float) -> Point:
        return (
            start[0] + dx * at + source.uniform(-0.15, 0.15) * (abs(dx) or 1.0),
            start[1] + dy * at + source.uniform(-0.15, 0.15) * (abs(dy) or 1.0),
        )

    return cubic_bezier_points(start, end, control(1 / 3), control(2 / 3), steps=steps)


def scroll_deltas(
    rng: random.Random | None = None,
    *,
    steps: int | None = None,
) -> list[int]:
    """Случайный скролл: 3–6 рывков по 80–320 px (параметры §3.3)."""
    source = rng if rng is not None else random
    count = steps if steps is not None else source.randint(SCROLL_STEPS_MIN, SCROLL_STEPS_MAX)
    return [source.randint(SCROLL_STEP_PX_MIN, SCROLL_STEP_PX_MAX) for _ in range(count)]


def reading_pause(rng: random.Random | None = None) -> float:
    """Небольшая пауза «чтения» страницы, сек (0.4–1.6 с, §3.3)."""
    source = rng if rng is not None else random
    return round(source.uniform(READING_PAUSE_MIN_SECONDS, READING_PAUSE_MAX_SECONDS), 3)
