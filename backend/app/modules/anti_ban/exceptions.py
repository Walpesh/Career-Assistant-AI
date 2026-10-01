"""Исключения Proxy & Anti-Ban Module (docs/04_PARSING_RULES.md §3, §5).

Внутренние ошибки модуля (не HTTP-контракт): Parsing Orchestrator ловит их
и переводит задачу в статус из docs/04 §5 (retry / waiting_captcha).
"""

from __future__ import annotations


class AntiBanError(Exception):
    """Базовая ошибка Proxy & Anti-Ban Module."""


class ProxyConfigError(AntiBanError):
    """Недопустимая конфигурация прокси (в т.ч. датацентровый — docs/04 §3.1)."""


class ProxyError(AntiBanError):
    """Сетевая ошибка соединения через прокси — сигнал к failover (смене IP)."""


class ProxyExhaustedError(ProxyError):
    """Нет живых прокси: все endpoints помечены мёртвыми или в недоступности."""


class CaptchaDetected(AntiBanError):
    """Капча Cloudflare/hh.ru → остановка воркера (docs/04 §5)."""

    #: Статус задачи парсинга по docs/04 §5 при появлении капчи.
    task_status: str = "waiting_captcha"

    def __init__(self, detail: str, *, task_status: str | None = None) -> None:
        super().__init__(detail)
        if task_status is not None:
            self.task_status = task_status


class RateLimitExceeded(AntiBanError):
    """HTTP 429/троттлинг не отступил за отведённые попытки (docs/04 §5 → retry)."""

    #: Статус задачи парсинга: вернуть в очередь на повтор.
    task_status: str = "retry"
