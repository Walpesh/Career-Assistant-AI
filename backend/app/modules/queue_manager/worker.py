"""Queue Manager — исполнение задач на ARQ-воркерах (docs/01 §3, docs/04 §6).

Архитектура: DB short-polling удалён. Задача создаётся в API, сразу
поставляется в очередь Redis через ``enqueue_job`` (queue_manager.queues),
откуда её забирает ARQ-воркер настоящим блокирующим чтением очереди
(ZRANGEBYSCORE по отсортированному множеству). Таблица `tasks` теперь —
источник состояния и прогресса, но не источник работы: в состоянии простоя
SELECT'ов к ней нет вообще.

Правила очереди, которые соблюдаются здесь (docs/04 §1, §6):

    - парсинг: не более ``MAX_CONCURRENT_PARSERS_PER_USER`` (2) задач
      одновременно на пользователя — держит Redis-семафор по ключу
      ``parsing:<user_id>``;
    - LLM: строго ``MAX_CONCURRENT_LLM_WORKERS`` (1) задача на всё приложение
      (очередь ``career:queue:llm``, max_jobs=1 + семафор ``llm``), поэтому
      параллельных вызовов Ollama не бывает (docs/05 §1);
    - задача, не получившая слот, не ждёт занятый ресурс, а возвращается в
      очередь Redis через ``Retry(defer=...)`` — то есть остаётся ``pending``.

Жизненный цикл записи в `tasks` (docs/02 §3.6, docs/03 §8) целиком обновляется
здесь, в обработчиках: ``pending → processing → completed / failed`` вместе с
``started_at`` / ``finished_at`` / прогрессом / ``result`` / ``error_message``
и публикацией событий в Realtime Module.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import Task
from app.modules.anti_ban import CaptchaDetected, RateLimitExceeded
from app.modules.anti_ban.exceptions import AntiBanError
from app.modules.parsing.service import ProgressReporter
from app.modules.queue_manager.queues import (
    LLM_QUEUE,
    LLM_TASK_TYPES,
    MAX_CONCURRENT_LLM_WORKERS,
    MAX_CONCURRENT_PARSERS_PER_USER,
    PARSING_QUEUE,
    PARSING_TASK_TYPES,
    TASK_PRIORITY,
    redis_settings,
)
from app.modules.queue_manager.slots import LLM_SLOT_GROUP, parsing_slot_group
from app.modules.realtime.bus import publish_event

__all__ = [
    "PARSING_TASK_TYPES",
    "LLM_TASK_TYPES",
    "TASK_PRIORITY",
    "MAX_CONCURRENT_PARSERS_PER_USER",
    "MAX_CONCURRENT_LLM_WORKERS",
    "TaskProgressReporter",
    "run_parsing_task",
    "run_llm_task",
    "ParsingQueueSettings",
    "LLMQueueSettings",
]

logger = logging.getLogger(__name__)


class TaskProgressReporter(ProgressReporter):
    """Прогресс задачи → БД + Realtime Module (docs/04 §6, docs/01 §5).

    Каждое обновление пишет current/total в `tasks` и публикует событие
    `task.progress` (docs/03 §8) в WebSocket пользователя.
    """

    def __init__(self, session: AsyncSession, task: Task) -> None:
        self._session = session
        self._task = task

    async def __call__(
        self,
        current: int,
        total: int,
        stage: str,
        message: str,
    ) -> None:
        self._task.progress_current = max(0, int(current))
        self._task.progress_total = max(0, int(total))
        await self._session.commit()
        await publish_event(
            str(self._task.user_id),
            "task.progress",
            {
                "task_id": str(self._task.id),
                "current": self._task.progress_current,
                "total": self._task.progress_total,
                "stage": stage,
                "message": message,
            },
        )


def _session_factory(ctx: dict) -> async_sessionmaker[AsyncSession]:
    """Фабрика сессий БД из контекста ARQ-job'ы."""
    factory = ctx.get("session_factory")
    if factory is None:
        from app.db.session import AsyncSessionLocal

        return AsyncSessionLocal
    return factory


def _slots(ctx: dict):
    """Ограничитель параллелизма из контекста job'ы.

    В ARQ-воркере ``ctx['redis']`` — пул Redis, поэтому семафор общий для всех
    процессов. Если Redis недоступен (юнит-тесты), используется внутрипроцессный
    счётчик с тем же интерфейсом.
    """
    existing = ctx.get("slots")
    if existing is not None:
        return existing

    from app.modules.queue_manager.slots import (
        InProcessQueueSlots,
        RedisQueueSlots,
    )

    redis = ctx.get("redis")
    slots = RedisQueueSlots(redis) if redis is not None else InProcessQueueSlots()
    ctx["slots"] = slots
    return slots


async def _acquire_slot(ctx: dict, group: str, limit: int, task_type: str, task_id: str):
    """Занять слот или вернуть задачу в очередь Redis (docs/04 §5).

    Если слоты заняты, задача остаётся ``pending`` и job откладывается через
    ``Retry(defer=...)`` — блокирующий воркер при этом освобождается.
    """
    slots = _slots(ctx)
    lease = await slots.acquire(group, limit)
    if lease is not None:
        return lease

    from arq.worker import Retry

    from app.core.config import settings

    logger.info(
        "Задача %s (%s): слоты группы %s заняты, возврат в очередь",
        task_id,
        task_type,
        group,
    )
    raise Retry(defer=max(1.0, settings.arq_poll_delay_seconds * 20))


async def _release_slot(ctx: dict, group: str, lease) -> None:
    if lease is None:
        return
    index, token = lease
    await _slots(ctx).release(group, index, token)


async def _start_task(session: AsyncSession, task: Task) -> bool:
    """Перевести задачу pending → processing и записать started_at (docs/02 §3.6).

    Returns False, если задача уже не pending (отменена или обработана).
    """
    if task.status != "pending":
        return False
    task.status = "processing"
    task.started_at = datetime.now(timezone.utc)
    await session.commit()
    await publish_event(
        str(task.user_id),
        "task.started",
        {
            "task_id": str(task.id),
            "task_type": task.task_type,
            "status": task.status,
        },
    )
    return True


async def _finish_completed(session: AsyncSession, task: Task, result: dict) -> None:
    """pending → processing → completed (docs/02 §3.6, docs/03 §8)."""
    task.status = "completed"
    task.result = result
    task.finished_at = datetime.now(timezone.utc)
    task.progress_current = int(result.get("total") or task.progress_current)
    await session.commit()
    await publish_event(
        str(task.user_id),
        "task.completed",
        {"task_id": str(task.id), "result": result},
    )


async def _finish_failed(
    session: AsyncSession,
    task: Task,
    error_message: str,
    *,
    popup: dict | None = None,
) -> None:
    """pending → processing → failed + уведомление (docs/03 §8)."""
    task.status = "failed"
    task.error_message = error_message
    task.finished_at = datetime.now(timezone.utc)
    await session.commit()
    await publish_event(
        str(task.user_id),
        "task.failed",
        {"task_id": str(task.id), "error": error_message},
    )
    if popup:
        await publish_event(str(task.user_id), "popup", popup)


async def run_parsing_task(ctx: dict, task_id: str) -> dict | None:
    """ARQ-job очереди парсинга: выполнить задачу одного пользователя.

    Ограничение «≤ 2 одновременных парсинг-воркера на пользователя»
    (docs/04 §1, §6) обеспечивается семафором ``parsing:<user_id>``; при
    занятых слотах job откладывается, и задача остаётся в ``pending``.
    """
    from app.db.models import Task as TaskModel

    factory = _session_factory(ctx)
    async with factory() as session:
        try:
            task = await session.get(TaskModel, uuid.UUID(str(task_id)))
        except (ValueError, TypeError):
            logger.warning("Некорректный task_id в job'е парсинга: %r", task_id)
            return None
        if task is None:
            logger.warning("Задача %s не найдена в БД — job пропущен", task_id)
            return None
        if task.task_type not in PARSING_TASK_TYPES:
            # docs/04 §6: LLM-задачи обслуживает только LLM-очередь.
            logger.warning("Задача %s (%s) не относится к очереди парсинга", task_id, task.task_type)
            return None

        group = parsing_slot_group(task.user_id)
        lease = await _acquire_slot(
            ctx, group, MAX_CONCURRENT_PARSERS_PER_USER, task.task_type, task_id
        )
        try:
            if not await _start_task(session, task):
                return None  # отменена или уже обработана
            return await _execute_parsing(session, task)
        finally:
            await _release_slot(ctx, group, lease)


async def _execute_parsing(session: AsyncSession, task: Task) -> dict:
    """Выполнить задачу парсинга и зафиксировать её итоговый статус."""
    reporter = TaskProgressReporter(session, task)
    try:
        outcome = await _dispatch_parsing(session, task, reporter)
    except CaptchaDetected as exc:
        # docs/04 §5: капча → остановка задачи, уведомление пользователя.
        await _finish_failed(
            session,
            task,
            f"Обнаружена капча hh.ru: {exc}. Требуется ручное вмешательство "
            f"(docs/04 §2 п.3)",
            popup={
                "type": "warning",
                "title": "Капча hh.ru",
                "message": "Парсинг остановлен: требуется ручное вмешательство",
            },
        )
        return {"failed": 1, "error": "captcha"}
    except RateLimitExceeded as exc:
        # docs/04 §5: 429 после retry → задача не выполнена (retry исчерпан).
        await _finish_failed(session, task, f"Превышен лимит запросов hh.ru: {exc}")
        return {"failed": 1, "error": str(exc)}
    except AntiBanError as exc:
        await _finish_failed(session, task, f"Ошибка обхода защиты hh.ru: {exc}")
        return {"failed": 1, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001 — задача не должна ронять воркер
        logger.exception("Задача %s завершилась с ошибкой", task.id)
        await _finish_failed(session, task, f"Ошибка парсинга: {exc}")
        return {"failed": 1, "error": str(exc)}

    result = outcome.as_dict()
    await _finish_completed(session, task, result)
    return result


async def _dispatch_parsing(
    session: AsyncSession, task: Task, reporter: ProgressReporter
):
    """Маршрутизация задачи в нужный режим оркестратора (docs/04 §4)."""
    from app.modules.parsing.service import ParsingOrchestrator
    from app.modules.vacancy_storage.service import extract_hh_vacancy_id

    payload = task.payload or {}
    orchestrator = ParsingOrchestrator()

    if task.task_type == "parse_auto":
        return await orchestrator.run_auto(
            session,
            user_id=task.user_id,
            keywords=list(payload.get("keywords") or []),
            employment_forms=list(payload.get("employment_forms") or []),
            work_formats=list(payload.get("work_formats") or []),
            schedules=list(payload.get("schedules") or []),
            max_pages=int(payload.get("max_pages") or 5),
            progress=reporter,
        )

    if task.task_type == "parse_group":
        return await orchestrator.run_group(
            session,
            user_id=task.user_id,
            search_url=str(payload.get("search_url") or ""),
            max_pages=int(payload.get("max_pages") or 5),
            progress=reporter,
        )

    if task.task_type == "parse_manual":
        vacancy_url = str(payload.get("vacancy_url") or "")
        return await orchestrator.run_manual(
            session,
            user_id=task.user_id,
            vacancy_url=vacancy_url,
            hh_vacancy_id=extract_hh_vacancy_id(vacancy_url),
            progress=reporter,
        )

    raise ValueError(f"Неизвестный тип задачи парсинга: {task.task_type}")


async def _llm_progress(
    session: AsyncSession, task: Task, current: int, total: int, message: str
) -> None:
    """Прогресс LLM-задачи → БД + Realtime (docs/03 §8, task.progress)."""
    task.progress_current = max(0, int(current))
    task.progress_total = max(0, int(total))
    await session.commit()
    await publish_event(
        str(task.user_id),
        "task.progress",
        {
            "task_id": str(task.id),
            "current": task.progress_current,
            "total": task.progress_total,
            "stage": "llm",
            "message": message,
        },
    )


async def run_llm_task(ctx: dict, task_id: str) -> dict | None:
    """ARQ-job очереди LLM: анализ/письмо/сжатие резюме (docs/04 §6).

    Очередь ``career:queue:llm`` обслуживается воркером с ``max_jobs=1``, а
    дополнительно действует семафор ``llm`` на 1 слот — это гарантирует, что
    Ollama никогда не вызывается параллельно, даже если процессов-воркеров
    несколько (docs/05 §1).
    """
    from app.db.models import Task as TaskModel

    factory = _session_factory(ctx)
    async with factory() as session:
        try:
            task = await session.get(TaskModel, uuid.UUID(str(task_id)))
        except (ValueError, TypeError):
            logger.warning("Некорректный task_id в job'е LLM: %r", task_id)
            return None
        if task is None:
            logger.warning("Задача %s не найдена в БД — job пропущен", task_id)
            return None
        if task.task_type not in LLM_TASK_TYPES:
            # docs/04 §6: парсинг обслуживается только очередью парсинга.
            logger.warning("Задача %s (%s) не относится к LLM-очереди", task_id, task.task_type)
            return None

        lease = await _acquire_slot(
            ctx, LLM_SLOT_GROUP, MAX_CONCURRENT_LLM_WORKERS, task.task_type, task_id
        )
        try:
            if not await _start_task(session, task):
                return None  # отменена или уже обработана
            if task.task_type == "convert_resume":
                return await _run_convert_resume(session, task)
            return await _run_llm_processing(session, task)
        finally:
            await _release_slot(ctx, LLM_SLOT_GROUP, lease)


async def _run_llm_processing(session: AsyncSession, task: Task) -> dict:
    """Обработать список вакансий задачи анализа/письма (docs/05 §4–§6)."""
    from app.modules.analysis_letter.llm import LLMError
    from app.modules.analysis_letter.service import MODE_BY_TASK_TYPE, process_vacancy

    payload = task.payload or {}
    mode = str(payload.get("mode") or MODE_BY_TASK_TYPE.get(task.task_type, "analyze"))
    raw_threshold = payload.get("match_threshold")
    threshold = int(raw_threshold) if isinstance(raw_threshold, int) else None
    raw_ids = [str(item) for item in (payload.get("vacancy_ids") or [])]

    outcomes: list[dict] = []
    errors: list[str] = []
    total = len(raw_ids) or 1
    task.progress_total = total
    task.progress_current = 0

    for index, raw_id in enumerate(raw_ids, start=1):
        try:
            vacancy_uuid = uuid.UUID(raw_id)
        except (ValueError, TypeError):
            errors.append(f"Некорректный vacancy_id в задаче: {raw_id}")
            continue

        async def report(message: str, i: int = index) -> None:
            await _llm_progress(session, task, i, total, message)

        try:
            outcome = await process_vacancy(
                session,
                user_id=task.user_id,
                vacancy_id=vacancy_uuid,
                mode=mode,
                match_threshold=threshold,
                progress=report,
            )
        except LLMError as exc:
            errors.append(str(exc))
            await session.commit()
            await _llm_progress(session, task, index, total, "Ошибка обработки")
            continue

        outcomes.append(outcome.as_dict())
        await session.commit()
        await _llm_progress(session, task, index, total, outcome.status)

    # docs/05 §7: если ни одна вакансия не обработана успешно —
    # задача переводится в failed (LLM недоступна / таймаут).
    succeeded = [o for o in outcomes if o["analyzed"] or o["letter_generated"]]
    if not succeeded:
        detail = errors or [err for o in outcomes for err in o.get("errors", [])]
        await _finish_failed(
            session, task, "; ".join(detail) or "Задача не содержит вакансий"
        )
        return {"failed": 1, "errors": detail}

    result = {
        "total": len(outcomes),
        "analyzed": sum(1 for o in outcomes if o["analyzed"]),
        "letters": sum(1 for o in outcomes if o["letter_generated"]),
        "mode": mode,
        "outcomes": outcomes,
    }
    if errors:
        result["errors"] = errors
    await _finish_completed(session, task, result)

    # События о готовых сущностях (docs/03 §8).
    for item in outcomes:
        await publish_event(
            str(task.user_id),
            "vacancy.updated",
            {"vacancy_id": item["vacancy_id"], "status": item["status"]},
        )
        if item["analyzed"]:
            await publish_event(
                str(task.user_id),
                "analysis.ready",
                {"vacancy_id": item["vacancy_id"], "match_score": item["match_score"]},
            )
        if item["letter_generated"]:
            await publish_event(
                str(task.user_id),
                "letter.ready",
                {"vacancy_id": item["vacancy_id"]},
            )
    return result

async def _run_convert_resume(session: AsyncSession, task: Task) -> dict:
    """Этап 0 LLM-пайплайна: сжатие резюме → user_profiles.compact_resume.

    Промпт и лимит COMPACT_RESUME_MAX_CHARS — docs/05 §3.
    """
    from app.core.config import settings
    from app.db.models import UserProfile
    from app.modules.user_profile.llm import (
        LLMError,
        compress_resume_text,
        trim_to_limit,
    )

    await _llm_progress(session, task, 0, 1, "Сжатие резюме")

    profile = await session.get(UserProfile, task.user_id)
    resume_text = ((profile.resume_text if profile else None) or "").strip()
    if not resume_text:
        await _finish_failed(
            session, task, "Сначала заполните resume_text — сжимать нечего"
        )
        return {"failed": 1, "error": "resume_empty"}

    try:
        compact = await compress_resume_text(
            resume_text, max_chars=settings.compact_resume_max_chars
        )
    except LLMError as exc:
        await _finish_failed(session, task, f"Не удалось сократить резюме: {exc}")
        return {"failed": 1, "error": str(exc)}

    if profile is not None:
        profile.compact_resume = trim_to_limit(
            compact, settings.compact_resume_max_chars
        )
        await session.commit()

    result = {
        "compact_resume_chars": len(profile.compact_resume or "") if profile else 0,
        "max_chars": settings.compact_resume_max_chars,
    }
    await _finish_completed(session, task, result)
    return result


# --- ARQ WorkerSettings (запуск воркеров отдельными процессами) ------------
# docs/01 §6: парсинг масштабируется горизонтально, LLM — строго один процесс.
#
#   arq app.modules.queue_manager.worker:ParsingQueueSettings
#   arq app.modules.queue_manager.worker:LLMQueueSettings


class _QueueWorkerSettingsBase:
    """Общая часть настроек воркера: очередь, Redis, таймауты."""

    queue_name: str = PARSING_QUEUE

    @staticmethod
    def _common() -> dict:
        from app.core.config import settings

        return {
            "redis_settings": redis_settings(),
            "job_timeout": settings.arq_job_timeout_seconds,
            "max_tries": settings.arq_max_tries,
            "poll_delay": settings.arq_poll_delay_seconds,
        }

    def __init__(self) -> None:
        from app.core.config import settings

        common = self._common()
        self.redis_settings = common["redis_settings"]
        self.job_timeout = common["job_timeout"]
        self.max_tries = common["max_tries"]
        self.poll_delay = common["poll_delay"]
        self.max_jobs = settings.arq_parsing_max_jobs

    @classmethod
    def as_worker_kwargs(cls) -> dict:
        """Параметры для создания arq.worker.Worker (используется в тестах)."""
        instance = cls()
        return {
            "queue_name": instance.queue_name,
            "functions": cls.functions,
            "max_jobs": instance.max_jobs,
            "redis_settings": instance.redis_settings,
            "job_timeout": instance.job_timeout,
            "max_tries": instance.max_tries,
            "poll_delay": instance.poll_delay,
        }


class ParsingQueueSettings(_QueueWorkerSettingsBase):
    """Воркер очереди парсинга (≤ 2 задач на пользователя — Redis-семафор)."""

    queue_name = PARSING_QUEUE
    functions = ["app.modules.queue_manager.worker.run_parsing_task"]


class LLMQueueSettings(_QueueWorkerSettingsBase):
    """Воркер очереди LLM — строго один поток на всё приложение (docs/04 §6)."""

    queue_name = LLM_QUEUE
    functions = ["app.modules.queue_manager.worker.run_llm_task"]

    def __init__(self) -> None:
        from app.core.config import settings

        super().__init__()
        # docs/05 §1: LLM-запросы никогда не выполняются параллельно.
        self.max_jobs = settings.arq_llm_max_jobs

