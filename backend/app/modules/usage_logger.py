"""Proxy Usage Logger — учёт прокси-трафика и предупреждения по капче.

TASK «Proxy Consumption & Cost Tracking»: считать объём трафика на задачу
и поднимать предупреждение, когда доля капчи превышает порог.

Зачем это нужно:

* **себестоимость.** Парсинг идёт через платные резидентные прокси, поэтому
  каждый мегабайт — деньги. Без учёта нельзя понять, во сколько обходится
  одна вакансия;
* **безопасность аккаунта.** Рост доли капчи означает, что hh.ru начал
  блокировать наш трафик. docs/04 §9 предписывает при доле капчи > 5%
  снижать интенсивность парсинга — без счётчика это условие выполнить
  нечем, поэтому логгер считает капчу и поднимает алерт.

Логгер пишет **одну строку на задачу** в `proxy_usage_logs` (docs/02 §3.11):
запись идемпотентна по ``task_id``, поэтому повторный вызов (например,
повтор задачи после снятия капчи) обновляет ту же строку, а не плодит
дубликаты.

Счётчик живёт в памяти процесса (воркера), а не в БД: писать в базу на
каждый HTTP-запрос означало бы тысячи INSERT'ов в минуту. В БД он
попадает один раз — по завершении задачи.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.logging import get_logger
from app.db.models import ProxyUsageLog

__all__ = [
    "MB",
    "ProxyUsage",
    "UsageLogger",
    "cost_kopecks",
    "logger_for_task",
]

log = get_logger(__name__)

#: Байт в мегабайте (для пересчёта трафика в квоту ``proxy_mb``).
MB = 1024 * 1024

#: Минимум ответов, после которого доля капчи вообще проверяется.
#: На первом же ответе любая капча даёт 100% и вызывала бы постоянные
#: ложные предупреждения.
_MIN_REQUESTS_FOR_ALERT = 10


def cost_kopecks(bytes_total: int) -> int:
    """Себестоимость трафика в копейках по ``PROXY_COST_PER_MB_KOPECKS``.

    Округление вверх: неполный мегабайт провайдер тоже тарифицирует, и
    «не списывать» его означало бы убыток, а не экономию.
    """
    megabytes = -(-max(0, int(bytes_total)) // MB)
    return megabytes * max(0, int(settings.proxy_cost_per_mb_kopecks))


@dataclass
class ProxyUsage:
    """Накопитель расхода трафика по одной задаче парсинга."""

    task_id: uuid.UUID
    user_id: uuid.UUID
    bytes_total: int = 0
    requests_total: int = 0
    captcha_total: int = 0

    def record(self, byte_count: int, *, captcha: bool = False) -> None:
        """Зафиксировать один ответ: объём в байтах и признак капчи."""
        self.bytes_total += max(0, int(byte_count))
        self.requests_total += 1
        if captcha:
            self.captcha_total += 1

    @property
    def megabytes(self) -> float:
        """Объём трафика в мегабайтах (для квоты ``proxy_mb``)."""
        return self.bytes_total / MB

    @property
    def captcha_rate(self) -> float:
        """Доля ответов с капчей (0..1); делить на ноль нельзя."""
        if self.requests_total <= 0:
            return 0.0
        return self.captcha_total / self.requests_total

    @property
    def cost_kopecks(self) -> int:
        """Себестоимость собранного трафика в копейках."""
        return cost_kopecks(self.bytes_total)

    def summary(self) -> dict:
        """Сводка по задаче — попадает в лог и в результат воркера."""
        return {
            "task_id": str(self.task_id),
            "bytes_total": self.bytes_total,
            "megabytes": round(self.megabytes, 3),
            "requests_total": self.requests_total,
            "captcha_total": self.captcha_total,
            "captcha_rate": round(self.captcha_rate, 4),
            "cost_kopecks": self.cost_kopecks,
        }


class UsageLogger:
    """Сборщик расхода прокси-трафика по задачам парсинга.

    Экземпляр живёт внутри одной задачи: счётчики изолированы, поэтому
    параллельные задачи разных пользователей не смешивают статистику.
    """

    def __init__(self, task_id: uuid.UUID, user_id: uuid.UUID) -> None:
        self.usage = ProxyUsage(task_id=task_id, user_id=user_id)
        #: Уже выданные предупреждения — чтобы не повторять их в лог.
        self._warned: set[str] = set()

    # --- сбор ---------------------------------------------------------------

    def record_fetch(self, byte_count: int, *, captcha: bool = False) -> None:
        """Учесть один ответ прокси-сессии."""
        self.usage.record(byte_count, captcha=captcha)
        if captcha:
            self._check_captcha_rate()

    def record_captcha(self) -> None:
        """Зафиксировать капчу (ответ-преграда, тело обычно пустое)."""
        self.usage.captcha_total += 1
        if self.usage.captcha_total > self.usage.requests_total:
            # Счётчик капч не может превышать число запросов: ограничение
            # ck_proxy_usage_logs_captcha_lte_requests отклонило бы INSERT.
            # Это защита от ошибки в вызывающем коде, а не норма сети.
            self.usage.requests_total = self.usage.captcha_total
        self._check_captcha_rate()

    def merge(self, other: ProxyUsage) -> None:
        """Добавить расход другой сессии той же задачи (ретрай после капчи)."""
        self.usage.bytes_total += other.bytes_total
        self.usage.requests_total += other.requests_total
        self.usage.captcha_total += other.captcha_total
        self._check_captcha_rate()

    # --- предупреждения -----------------------------------------------------

    def _check_captcha_rate(self) -> bool:
        """Предупредить, если доля капчи выше порога (docs/04 §9)."""
        threshold = float(settings.proxy_alert_captcha_rate_threshold)
        rate = self.usage.captcha_rate
        if self.usage.requests_total < _MIN_REQUESTS_FOR_ALERT or rate <= threshold:
            return False
        if "captcha_rate" in self._warned:
            return False
        self._warned.add("captcha_rate")
        log.warning(
            "proxy_usage: доля капчи выше порога — снижаем интенсивность",
            task_id=str(self.usage.task_id),
            captcha_rate=round(rate, 4),
            threshold=threshold,
            captcha_total=self.usage.captcha_total,
            requests_total=self.usage.requests_total,
        )
        return True

    def _check_volume(self) -> bool:
        """Предупредить об аномальном объёме трафика на одну задачу."""
        threshold = int(settings.proxy_alert_mb_per_task_threshold)
        if self.usage.megabytes <= threshold or "volume" in self._warned:
            return False
        self._warned.add("volume")
        log.warning(
            "proxy_usage: объём трафика задачи выше порога",
            task_id=str(self.usage.task_id),
            megabytes=round(self.usage.megabytes, 2),
            threshold_mb=threshold,
        )
        return True

    def warnings(self) -> list[str]:
        """Сработавшие предупреждения — попадают в результат задачи (UI)."""
        result: list[str] = []
        if "captcha_rate" in self._warned:
            result.append(
                f"Доля капчи {self.usage.captcha_rate:.1%} выше порога "
                f"{float(settings.proxy_alert_captcha_rate_threshold):.1%} — "
                "интенсивность парсинга снижена"
            )
        if "volume" in self._warned:
            result.append(
                f"Объём трафика задачи {self.usage.megabytes:.1f} МБ выше порога "
                f"{int(settings.proxy_alert_mb_per_task_threshold)} МБ"
            )
        return result

    async def flush(self, db: AsyncSession) -> ProxyUsage | None:
        """Записать накопленный расход трафика в БД (одна строка на задачу).

        Returns:
            Итоговый снимок расхода либо ``None``, если запись отключена
            (``PROXY_USAGE_PERSIST=false``) или не удалась.
        """
        self._check_captcha_rate()
        self._check_volume()

        if not settings.proxy_usage_persist:
            log.info("proxy_usage: запись в БД отключена", **self.usage.summary())
            return self.usage

        record = ProxyUsageLog(
            user_id=self.usage.user_id,
            task_id=self.usage.task_id,
            bytes_total=self.usage.bytes_total,
            requests_total=self.usage.requests_total,
            captcha_total=self.usage.captcha_total,
        )
        try:
            db.add(record)
            await db.commit()
        except Exception:  # noqa: BLE001 — учёт трафика не должен ронять задачу
            await db.rollback()
            log.warning(
                "proxy_usage: не удалось записать расход трафика",
                task_id=str(self.usage.task_id),
                exc_info=True,
            )
            return None

        log.info("proxy_usage: расход записан", **self.usage.summary())
        return self.usage


def logger_for_task(task_id: uuid.UUID, user_id: uuid.UUID) -> UsageLogger:
    """Создать логгер расхода прокси-трафика для задачи парсинга."""
    return UsageLogger(task_id=task_id, user_id=user_id)
