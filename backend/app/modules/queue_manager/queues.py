"""Queue Manager — шина задач на Redis + ARQ (docs/01 §3, docs/04 §6).

Модуль отвечает за постановку задач в очередь и за подключение пула Redis:

    - ``get_pool()``      — единый ArqRedis-пул приложения (ленивое создание);
    - ``enqueue_task()``  — постановка задачи в нужную очередь сразу при
      создании задачи в API (никакого опроса таблицы `tasks`);
    - ``start_embedded_workers()`` — подъём ARQ-воркеров внутри процесса
      FastAPI (для dev/однопроцессного развёртывания);
    - ``recover_pending_tasks()`` — возврат в очередь задач, застрявших в
      pending/processing после аварийной остановки.

Две независимые очереди (docs/04 §6):

    ``career:queue:parsing`` — парсинг, до 2 воркеров на пользователя;
    ``career:queue:llm``     — LLM, строго 1 воркер на всё приложение.

Механизм доставки задач — родной для ARQ: отсортированное множество Redis
и ``ZRANGEBYSCORE`` в воркере (по сути блокирующее чтение очереди). База
данных в этом цикле не участвует: в состоянии простоя SQL-запросов нет.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from arq.connections import ArqRedis, RedisSettings, create_pool
from arq.utils import timestamp_ms, to_ms, to_unix_ms

from app.core.config import settings

__all__ = [
    "PARSING_QUEUE",
    "LLM_QUEUE",
    "PARSING_JOB",
    "LLM_JOB",
    "RECOVER_LOCK_KEY",
    "QueueUnavailable",
    "redis_settings",
    "get_pool",
    "set_pool",
    "close_pool",
    "queue_for_task_type",
    "enqueue_task",
    "abort_job",
    "acquire_recover_lock",
    "recover_pending_tasks",
    "start_embedded_workers",
    "stop_embedded_workers",
    "RecordingPool",
]

logger = logging.getLogger(__name__)

#: Имена очередей Redis (docs/04 §6).
PARSING_QUEUE = settings.arq_queue_parsing
LLM_QUEUE = settings.arq_queue_llm

#: Полные пути job-функций ARQ — по ним воркер находит функцию (arq func).
PARSING_JOB = "app.modules.queue_manager.worker.run_parsing_task"
LLM_JOB = "app.modules.queue_manager.worker.run_llm_task"

#: Типы задач парсинга (docs/02 §3.6).
PARSING_TASK_TYPES: frozenset[str] = frozenset(
    {"parse_auto", "parse_group", "parse_manual"}
)

#: Типы LLM-задач — строгая очередь (docs/04 §6, docs/01 §5).
LLM_TASK_TYPES: frozenset[str] = frozenset(
    {"analyze", "generate_letter", "auto_full", "convert_resume"}
)

#: Приоритеты парсинга (docs/04 §6): ручное → групповое → авто (меньше = выше).
TASK_PRIORITY: dict[str, int] = {
    "parse_manual": 0,
    "parse_group": 1,
    "parse_auto": 2,
}

#: «Максимум 2 одновременных парсинг-воркера» на пользователя (docs/04 §1, §6).
MAX_CONCURRENT_PARSERS_PER_USER = 2

#: LLM-очередь — строго один воркер на всё приложение (docs/04 §6).
MAX_CONCURRENT_LLM_WORKERS = 1

#: Redis-ключ распределённой блокировки восстановления задач.
#: Только один процесс (API или worker-parsing) выполняет
#: `recover_pending_tasks` — остальные пропускают (SET NX EX).
RECOVER_LOCK_KEY = "career:queue:recover:lock"


class QueueUnavailable(RuntimeError):
    """Redis-очередь недоступна — задачу не удалось поставить в обработку."""


_pool: ArqRedis | None = None
_pool_lock = asyncio.Lock()


def redis_settings() -> RedisSettings:
    """Параметры подключения к Redis из настроек приложения (app.core.config)."""
    return RedisSettings.from_dsn(settings.redis_url)


async def get_pool() -> ArqRedis | None:
    """Лениво создать и вернуть общий ArqRedis-пул (None, если Redis недоступен)."""
    global _pool
    if _pool is not None:
        return _pool
    async with _pool_lock:
        if _pool is None:
            try:
                _pool = await create_pool(redis_settings())
                logger.info("Queue Manager: пул Redis создан (%s)", settings.redis_url)
            except Exception as exc:  # noqa: BLE001 — приложение должно стартовать
                logger.error("Queue Manager: Redis недоступен (%s)", exc)
                return None
    return _pool


def set_pool(pool: ArqRedis | None) -> None:
    """Подставить пул вручную (используется в тестах)."""
    global _pool
    _pool = pool


async def close_pool() -> None:
    """Закрыть общий пул Redis (lifespan/shutdown)."""
    global _pool
    async with _pool_lock:
        if _pool is not None:
            try:
                await _pool.aclose()
            except Exception:  # noqa: BLE001 — закрытие не должно ронять shutdown
                logger.debug("Queue Manager: ошибка закрытия пула Redis", exc_info=True)
            _pool = None


def queue_for_task_type(task_type: str) -> str:
    """Очередь Redis для типа задачи (docs/04 §6: парсинг и LLM разделены)."""
    return LLM_QUEUE if task_type in LLM_TASK_TYPES else PARSING_QUEUE


def _job_for_task_type(task_type: str) -> str:
    return LLM_JOB if task_type in LLM_TASK_TYPES else PARSING_JOB


#: Окно приоритета парсинга в очереди Redis, мс (docs/04 §6).
#:
#: ARQ выбирает job'ы по возрастанию score, а score задаётся временем постановки
#: в очередь. Чтобы сохранить приоритет «ручное → групповое → авто» (docs/04 §6),
#: score сдвигается назад на ``PRIORITY_WINDOW_MS * (len - rank)``. Все значения
#: остаются в прошлом, поэтому задача по-прежнему берётся немедленно — меняется
#: только порядок внутри короткого окна постановки.
PRIORITY_WINDOW_MS = 5_000


def _priority_offset_ms(task_type: str) -> int:
    """Сдвиг score в миллисекундах: меньше — выше приоритет в очереди."""
    rank = TASK_PRIORITY.get(task_type)
    if rank is None:
        return 0  # LLM-задачи не смещаются
    return PRIORITY_WINDOW_MS * (max(TASK_PRIORITY.values()) - rank)


async def enqueue_task(
    task_id: uuid.UUID | str,
    task_type: str,
    *,
    attempt: str | None = None,
    pool: ArqRedis | None = None,
) -> str | None:
    """Поставить задачу в очередь Redis через ARQ (enqueue_job).

    Вызывается из API сразу после создания записи в `tasks` — это единственный
    путь появления новой работы, поэтому БД опрашивать не требуется.

    Args:
        attempt: уникальный суффикс ``_job_id``. Нужен, когда стандартная
            дедупликация мешает пере-постановке: ключ ``arq:job:<task_type>:<id>``
            (или ``arq:result:...`` завершённого job'а) ещё жив в Redis, хотя
            сама задача в БД снова pending (восстановление после сбоя, resume).

    Returns:
        Идентификатор job'а в Redis либо None, если задача уже была в очереди
        (enqueue_job дедуплицирует по ``_job_id``).
    """
    redis = pool or await get_pool()
    if redis is None:
        raise QueueUnavailable("Redis-очередь недоступна")

    job_id = f"{task_type}:{task_id}"
    if attempt:
        job_id = f"{job_id}:{attempt}"

    kwargs: dict[str, Any] = {
        "_queue_name": queue_for_task_type(task_type),
        # Стабильный job_id = дедупликация: повторная постановка той же задачи
        # не создаст вторую работу.
        "_job_id": job_id,
    }
    # Приоритет парсинга (docs/04 §6) задаётся score'ом в отсортированном
    # множестве Redis — самой очередью, без обращения к БД.
    offset = _priority_offset_ms(task_type)
    if offset:
        kwargs["_defer_until"] = datetime.fromtimestamp(
            (timestamp_ms() - offset) / 1000, tz=timezone.utc
        )

    job = await redis.enqueue_job(_job_for_task_type(task_type), str(task_id), **kwargs)
    if job is None:
        logger.info("Задача %s уже была в очереди Redis", task_id)
    else:
        logger.info(
            "Задача %s (%s) поставлена в очередь %s, job=%s",
            task_id,
            task_type,
            queue_for_task_type(task_type),
            job.job_id,
        )
    return job.job_id if job else None


async def abort_job(
    task_id: uuid.UUID | str,
    task_type: str,
    *,
    attempt: str | None = None,
    pool: ArqRedis | None = None,
) -> bool:
    """Явно снять job задачи из очереди Redis (отмена/пере-постановка).

    ARQ 0.28 не даёт готового ``job_abort``, поэтому удаляются ключи самого job'а
    (``arq:job:…``), его результата (``arq:result:…`` — иначе дедупликация не
    пропустит новую постановку) и запись из отсортированного множества очереди.

    Returns:
        True, если из очереди был удалён хотя бы один ключ/job.
    """
    from arq.constants import job_key_prefix, result_key_prefix

    redis = pool or await get_pool()
    if redis is None:
        return False

    job_id = f"{task_type}:{task_id}"
    if attempt:
        job_id = f"{job_id}:{attempt}"

    try:
        custom_abort = getattr(redis, "job_abort", None)
        if custom_abort is not None:  # пулы-заглушки в тестах
            return bool(await custom_abort(job_id))
        removed = await redis.delete(job_key_prefix + job_id, result_key_prefix + job_id)
        await redis.zrem(queue_for_task_type(task_type), job_id)
        return bool(removed)
    except Exception:  # noqa: BLE001 — отмена не должна ронять запрос/старт
        logger.debug("Не удалось снять job %s из очереди Redis", job_id, exc_info=True)
        return False


async def acquire_recover_lock(
    redis: Any,
    *,
    ttl_seconds: int | None = None,
) -> bool:
    """Захватить распределённую блокировку восстановления (Redis SET NX EX).

    Returns True, если блокировка захвачена текущим процессом — только он
    выполняет `recover_pending_tasks`. False — другой процесс уже
    восстанавливает (или недавно восстановил) — пропуск без дублей.

    `RecordingPool` в тестах не имеет `set(..., nx=True)` — для него
    используется локальный флаг, сохраняющий контракт метода.
    """
    key = RECOVER_LOCK_KEY
    if ttl_seconds is not None:
        ttl = max(1, int(ttl_seconds))
    else:
        ttl = max(1, int(settings.queue_recover_lock_ttl_seconds))
    try:
        setter = getattr(redis, "set", None)
        if setter is None:
            return True
        # ArqRedis/redis.asyncio поддерживают SET NX EX — атомарный захват.
        acquired = await setter(key, "1", nx=True, ex=ttl)
        # fakeredis/некоторые стабы могут вернуть True/None вместо bool.
        return bool(acquired)
    except Exception:  # noqa: BLE001 — блокировка не должна ронять старт
        logger.debug("Не удалось захватить блокировку восстановления %s", key, exc_info=True)
        return True


async def recover_pending_tasks(
    session_factory: Any,
    *,
    pool: ArqRedis | None = None,
) -> int:
    """Вернуть в очередь задачи, застрявшие в pending/processing после сбоя.

    Выполняется один раз при старте приложения (docs/04 §6): это не опрос, а
    восстановление после аварийной остановки. Задача, начатая до сбоя,
    возвращается в исходное состояние ``pending`` — её обработка начнётся
    заново, ``started_at`` проставится заново.

    Защита от параллельного запуска несколькими репликами (production:
    api ×N + worker-parsing ×N): Redis-блокировка ``SET NX EX``
    (см. ``acquire_recover_lock``). Процесс, не захвативший блокировку,
    возвращает 0 без обращения к БД.

    Если ``enqueue_task`` вернул None (ARQ-дедупликация по ``_job_id``: ключ
    зависшего job'а ещё жив в Redis), job явно снимается через ``abort_job`` и
    задача ставится заново с уникальным суффиксом попытки — иначе задача
    потерялась бы: в БД она pending, а в очереди её больше никто не ждёт.
    """
    from sqlalchemy import select

    from app.db.models import Task

    redis = pool or await get_pool()
    if redis is None:
        logger.warning("Queue Manager: восстановление пропущено — Redis недоступен")
        return 0

    if not await acquire_recover_lock(redis):
        logger.info("Queue Manager: восстановление пропущено — занято другим процессом")
        return 0

    restored = 0
    async with session_factory() as session:
        tasks = list(
            (
                await session.scalars(
                    select(Task).where(Task.status.in_((("pending", "processing"))))
                )
            ).all()
        )
        for task in tasks:
            if task.status == "processing":
                task.status = "pending"
                task.started_at = None
            try:
                job_id = await enqueue_task(task.id, task.task_type, pool=redis)
                if job_id is None:
                    # Дедупликация по _job_id → снимаем «старый» job и
                    # пере-ставляем с уникальным attempt-суффиксом.
                    await abort_job(task.id, task.task_type, pool=redis)
                    job_id = await enqueue_task(
                        task.id,
                        task.task_type,
                        attempt=f"recover-{uuid.uuid4().hex[:8]}",
                        pool=redis,
                    )
            except QueueUnavailable:
                logger.warning("Задача %s не восстановлена — Redis недоступна", task.id)
                break
            if job_id is None:
                logger.warning(
                    "Задача %s не восстановлена: job не удалось поставить в очередь",
                    task.id,
                )
                continue
            restored += 1
        if restored:
            await session.commit()

    if restored:
        logger.info("Queue Manager: в очередь возвращено задач: %d", restored)
    return restored


# --- встроенные воркеры (lifespan FastAPI) --------------------------------

_embedded_workers: list[asyncio.Task] = []


async def start_embedded_workers(session_factory: Any = None) -> list[asyncio.Task]:
    """Поднять ARQ-воркеры очередей парсинга и LLM внутри процесса FastAPI.

    Альтернатива — отдельные процессы (docs/01 §6):

        arq app.modules.queue_manager.worker:ParsingQueueSettings
        arq app.modules.queue_manager.worker:LLMQueueSettings
    """
    from arq.worker import Worker

    from app.db.session import AsyncSessionLocal

    redis = await get_pool()
    if redis is None:
        logger.warning("Queue Manager: встроенные воркеры не запущены — Redis недоступен")
        return []

    factory = session_factory or AsyncSessionLocal
    shared = {
        "redis_pool": redis,
        "ctx": {"session_factory": factory},
        "job_timeout": settings.arq_job_timeout_seconds,
        "max_tries": settings.arq_max_tries,
        "poll_delay": settings.arq_poll_delay_seconds,
        # Воркер живёт внутри FastAPI: обработку SIGINT/SIGTERM ведёт uvicorn.
        "handle_signals": False,
    }

    workers = [
        Worker(
            functions=[PARSING_JOB],
            queue_name=PARSING_QUEUE,
            max_jobs=settings.arq_parsing_max_jobs,
            **shared,
        ),
        Worker(
            functions=[LLM_JOB],
            queue_name=LLM_QUEUE,
            # docs/04 §6: строго один LLM-воркер на всё приложение.
            max_jobs=settings.arq_llm_max_jobs,
            **shared,
        ),
    ]

    for worker in workers:
        _embedded_workers.append(asyncio.create_task(worker.async_run()))

    logger.info(
        "Queue Manager: ARQ-воркеры запущены (%s max_jobs=%d; %s max_jobs=%d)",
        PARSING_QUEUE,
        settings.arq_parsing_max_jobs,
        LLM_QUEUE,
        settings.arq_llm_max_jobs,
    )
    return list(_embedded_workers)


async def stop_embedded_workers() -> None:
    """Остановить встроенные ARQ-воркеры (lifespan/shutdown)."""
    tasks, _embedded_workers[:] = list(_embedded_workers), []
    for task in tasks:
        task.cancel()
    for task in tasks:
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001 — остановка не должна ронять приложение
            logger.debug("Queue Manager: ошибка остановки воркера", exc_info=True)
    if tasks:
        logger.info("Queue Manager: ARQ-воркеры остановлены")


# --- тестовый помощник ----------------------------------------------------


def _score_of(kwargs: dict) -> float:
    """Score job'а так же, как это делает ARQ в Redis (см. enqueue_job)."""
    defer_until = kwargs.get("_defer_until")
    if defer_until is not None:
        return to_unix_ms(defer_until)
    defer_by = kwargs.get("_defer_by")
    return float(timestamp_ms() + (to_ms(defer_by) or 0))


class _StubJob:
    """Минимальный аналог arq.jobs.Job, возвращаемого enqueue_job."""

    __slots__ = ("job_id",)

    def __init__(self, job_id: str) -> None:
        self.job_id = job_id


class RecordingPool:
    """Пул-заглушка: запоминает постановку задач вместо обращения к Redis.

    Нужен тестам очереди, чтобы проверять маршрутизацию задач в правильную
    очередь и дедупликацию по ``_job_id``, не поднимая Redis. Реальные job'ы
    при этом исполняются напрямую через ``run_parsing_task`` / ``run_llm_task``.
    """

    def __init__(self) -> None:
        self.jobs: list[dict] = []
        self.published: list[str] = []
        self._kv: dict[str, str] = {}

    async def enqueue_job(self, function: str, *args, **kwargs) -> "_StubJob | None":
        job_id = kwargs.get("_job_id") or f"job-{len(self.jobs)}"
        if any(job["job_id"] == job_id for job in self.jobs):
            return None  # дедупликация, как в настоящем enqueue_job
        self.jobs.append(
            {
                "job_id": job_id,
                "function": function,
                "args": args,
                "queue_name": kwargs.get("_queue_name"),
                # score из реального Redis-отсортированного множества:
                # задачи с меньшим score достаются воркеру раньше.
                "score": _score_of(kwargs),
            }
        )
        # В Redis отсортированное множество отдаёт job'ы по возрастанию score.
        self.jobs.sort(key=lambda job: job["score"])
        return _StubJob(job_id)

    def queue_len(self, queue_name: str) -> int:
        return sum(1 for job in self.jobs if job["queue_name"] == queue_name)

    async def job_abort(self, job_id: str) -> bool:
        """Снять job из очереди — аналог отмены в настоящем Redis (abort_job)."""
        before = len(self.jobs)
        self.jobs = [job for job in self.jobs if job["job_id"] != job_id]
        return len(self.jobs) != before

    async def publish(self, channel: str, message: str) -> None:
        """События Realtime Module пишутся в список (шина Redis в тестах)."""
        self.published.append(message)

    async def set(self, key: str, value: str, *, nx: bool = False, ex: int | None = None) -> bool:
        """Эмуляция Redis SET NX EX для `acquire_recover_lock` в тестах."""
        _ = ex  # TTL в заглушке не истекает — достаточно одноразового захвата
        if nx and key in self._kv:
            return False
        self._kv[key] = value
        return True
