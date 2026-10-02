"""Тесты Analysis & Letter Module: LLM-этапы docs/05 §4–§6 и LLM-очередь.

Сеть и модель не используются: Ollama подменяется на уровне
`analysis_letter.llm._generate`. Покрытие:
    §4 — разбор JSON-ответа, нормализация списков, clamp match_score 0..100,
          повтор при невалидном JSON (до 2 раз), ошибка после исчерпания попыток;
    §5 — генерация письма, чистка markdown-обёрток, проверка длины;
    §6 — режим AUTO: анализ → порог → письмо; ниже порога письма нет;
    §9 — версионирование писем (version = previous + 1);
    docs/04 §6 — LLM-очередь: строго одна задача одновременно, парсинг её не трогает.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import Analysis, CoverLetter, Task, User, UserProfile, Vacancy
from app.modules.analysis_letter import llm as llm_module
from app.modules.analysis_letter.llm import LLMError
from app.modules.analysis_letter.service import MODE_BY_TASK_TYPE, process_vacancy
from app.modules.queue_manager.slots import LLM_SLOT_GROUP
from app.modules.queue_manager.worker import (
    LLM_TASK_TYPES,
    MAX_CONCURRENT_LLM_WORKERS,
    run_llm_task,
)

COMPACT = "Python-разработчик, 6 лет опыта, FastAPI, PostgreSQL, Docker."

VACANCY_FIELDS = {
    "title": "Senior Python разработчик",
    "company_name": "ООО Ромашка",
    "salary_from": 250000,
    "salary_to": 350000,
    "experience": "Опыт работы: 3–6 лет",
    "employment_form": "полная занятост��",
    "work_format": "удалённая работа",
    "description_raw": "Требуется опыт с FastAPI и PostgreSQL, разработка микросервисов.",
}

GOOD_ANALYSIS = (
    '{"match_score": 82, "strengths": ["Опыт FastAPI"], '
    '"weaknesses": ["Нет Kubernetes"], "summary": "Хорошее соответствие."}'
)


# --- фикстуры -----------------------------------------------------------------

@pytest.fixture
def fake_llm(monkeypatch):
    """Подмена Ollama: очередь ответов + журнал вызовов."""
    state = {"responses": [], "calls": [], "default": GOOD_ANALYSIS}

    async def _fake_generate(prompt: str, *, temperature: float, timeout=None) -> str:
        state["calls"].append({"prompt": prompt, "temperature": temperature})
        if state["responses"]:
            return state["responses"].pop(0)
        return state["default"]

    monkeypatch.setattr(llm_module, "_generate", _fake_generate)
    return state


@pytest.fixture
def setup_user(engine):
    """Пользователь с compact_resume + вакансия для обработки."""

    async def _make(*, match_threshold: int = 70, compact: str = COMPACT):
        factory = async_sessionmaker(
            bind=engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
        )
        async with factory() as session:
            user = User(email=f"a{uuid.uuid4().hex[:10]}@test.dev", password_hash="x")
            session.add(user)
            await session.flush()
            session.add(
                UserProfile(
                    user_id=user.id,
                    full_name="Тест Кандидат",
                    compact_resume=compact,
                    match_threshold=match_threshold,
                )
            )
            vacancy = Vacancy(
                user_id=user.id,
                hh_vacancy_id=str(uuid.uuid4().int % 10**9),
                url="https://hh.ru/vacancy/1",
                title=VACANCY_FIELDS["title"],
                company_name=VACANCY_FIELDS["company_name"],
                description_raw=VACANCY_FIELDS["description_raw"],
                experience=VACANCY_FIELDS["experience"],
                work_format=VACANCY_FIELDS["work_format"],
                status="raw",
                source="manual",
            )
            session.add(vacancy)
            await session.commit()
            return user.id, vacancy.id

    return _make
# --- docs/05 §4: разбор и нормализация ---------------------------------------

async def test_analyze_parses_json_and_normalizes(fake_llm):
    result = await llm_module.analyze_vacancy(COMPACT, VACANCY_FIELDS)

    assert result.match_score == 82
    assert result.strengths == ["Опыт FastAPI"]
    assert result.weaknesses == ["Нет Kubernetes"]
    assert result.summary == "Хорошее соответствие."
    # docs/05 §8: анализ — более детерминированная температура.
    assert fake_llm["calls"][0]["temperature"] == 0.35
    # Промпт содержит данные кандидата и вакансии.
    assert COMPACT in fake_llm["calls"][0]["prompt"]
    assert VACANCY_FIELDS["title"] in fake_llm["calls"][0]["prompt"]


async def test_analyze_accepts_wrapped_json(fake_llm):
    """Модель часто оборачивает JSON в ```-блок — парсер должен это снять."""
    fake_llm["default"] = f"```json\n{GOOD_ANALYSIS}\n```"
    result = await llm_module.analyze_vacancy(COMPACT, VACANCY_FIELDS)
    assert result.match_score == 82


async def test_analyze_normalizes_string_lists(fake_llm):
    """Строки вместо массивов приводятся к спискам пунктов."""
    fake_llm["default"] = (
        '{"match_score": 70, "strengths": "Пункт один; Пункт два", '
        '"weaknesses": "- Минус один", "summary": "Итог."}'
    )
    result = await llm_module.analyze_vacancy(COMPACT, VACANCY_FIELDS)
    assert result.strengths == ["Пункт один", "Пункт два"]
    assert result.weaknesses == ["Минус один"]


async def test_analyze_clamps_match_score_to_db_range(fake_llm):
    """CHECK-ограничение БД: match_score 0..100 (docs/02 §3.4)."""
    fake_llm["default"] = '{"match_score": 180, "strengths": ["s"], "weaknesses": ["w"], "summary": "x"}'
    assert (await llm_module.analyze_vacancy(COMPACT, VACANCY_FIELDS)).match_score == 100

    fake_llm["default"] = '{"match_score": -20, "strengths": ["s"], "weaknesses": ["w"], "summary": "x"}'
    assert (await llm_module.analyze_vacancy(COMPACT, VACANCY_FIELDS)).match_score == 0


async def test_analyze_retries_invalid_json_then_succeeds(fake_llm):
    """docs/05 §7: невалидный JSON → повторный запрос с жёстким промптом."""
    fake_llm["responses"] = ["не JSON вовсе", GOOD_ANALYSIS]

    result = await llm_module.analyze_vacancy(COMPACT, VACANCY_FIELDS)

    assert result.match_score == 82
    assert len(fake_llm["calls"]) == 2
    # Вторая попытка содержит жёсткое требование вернуть только JSON.
    assert "ТОЛЬКО валидный JSON" in fake_llm["calls"][1]["prompt"]
    assert result.match_details["attempts"] == 2


async def test_analyze_fails_after_max_attempts(fake_llm):
    """docs/05 §7: исчерпание попыток → LLMError → задача failed."""
    fake_llm["responses"] = ["мусор"] * llm_module.MAX_JSON_ATTEMPTS
    with pytest.raises(LLMError):
        await llm_module.analyze_vacancy(COMPACT, VACANCY_FIELDS)
    assert len(fake_llm["calls"]) == llm_module.MAX_JSON_ATTEMPTS


async def test_analyze_requires_compact_resume(fake_llm):
    """Без compact_resume анализ бессмысленен (docs/05 §1)."""
    with pytest.raises(LLMError):
        await llm_module.analyze_vacancy("   ", VACANCY_FIELDS)
    assert fake_llm["calls"] == []


# --- docs/05 §5: письмо --------------------------------------------------------

async def test_generate_letter_returns_clean_text(fake_llm):
    letter = "Здравствуйте! " * 30
    fake_llm["default"] = f"```\n{letter}\n```"

    result = await llm_module.generate_cover_letter(COMPACT, VACANCY_FIELDS)

    assert result.startswith("Здравствуйте!")
    assert "```" not in result
    # docs/05 §8: письма — более «живая» температура.
    assert fake_llm["calls"][0]["temperature"] == 0.55


async def test_generate_letter_rejects_too_short(fake_llm):
    fake_llm["default"] = "Коротко."
    with pytest.raises(LLMError):
        await llm_module.generate_cover_letter(COMPACT, VACANCY_FIELDS)
# --- docs/05 §6 / §9: сервис обработки ----------------------------------------

async def test_mode_analyze_saves_analysis_and_updates_vacancy(fake_llm, setup_user, engine):
    user_id, vacancy_id = await setup_user()
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)

    async with factory() as session:
        outcome = await process_vacancy(
            session, user_id=user_id, vacancy_id=vacancy_id, mode="analyze"
        )

    assert outcome.analyzed is True
    assert outcome.letter_generated is False
    assert outcome.match_score == 82

    async with factory() as session:
        analysis = await session.scalar(select(Analysis).where(Analysis.vacancy_id == vacancy_id))
        vacancy = await session.get(Vacancy, vacancy_id)
        letter = await session.scalar(select(CoverLetter).where(CoverLetter.vacancy_id == vacancy_id))

    assert analysis is not None and analysis.match_score == 82
    assert analysis.match_details["model"]  # JSONB заполнен
    assert "Опыт FastAPI" in analysis.strengths
    assert vacancy.match_score == 82
    assert vacancy.status == "analyzed"
    assert letter is None  # режим analyze письма не создаёт


async def test_mode_letter_writes_letter_without_analysis(fake_llm, setup_user, engine):
    user_id, vacancy_id = await setup_user()
    letter_text = ("Здравствуйте! Готов обсудить детали. " * 10).strip()
    fake_llm["default"] = letter_text
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)

    async with factory() as session:
        outcome = await process_vacancy(
            session, user_id=user_id, vacancy_id=vacancy_id, mode="letter"
        )

    assert outcome.letter_generated is True and outcome.analyzed is False

    async with factory() as session:
        letter = await session.scalar(select(CoverLetter).where(CoverLetter.vacancy_id == vacancy_id))
        vacancy = await session.get(Vacancy, vacancy_id)

    assert letter is not None and letter.version == 1
    # Пост-обработка (docs/05 §7) убирает markdown-обёртки и лишние пробелы.
    assert letter.content == letter_text
    assert "```" not in letter.content
    assert vacancy.status == "letter_ready"


async def test_mode_auto_generates_letter_above_threshold(fake_llm, setup_user, engine):
    """docs/05 §6: score ≥ порога → анализ + письмо, статус letter_ready."""
    user_id, vacancy_id = await setup_user(match_threshold=70)
    fake_llm["responses"] = [GOOD_ANALYSIS, "Здравствуйте! " * 30]
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)

    async with factory() as session:
        outcome = await process_vacancy(
            session, user_id=user_id, vacancy_id=vacancy_id, mode="auto"
        )

    assert outcome.analyzed and outcome.letter_generated
    assert outcome.status == "letter_ready"

    async with factory() as session:
        vacancy = await session.get(Vacancy, vacancy_id)
        letter = await session.scalar(select(CoverLetter).where(CoverLetter.vacancy_id == vacancy_id))
    assert vacancy.status == "letter_ready"
    assert letter is not None


async def test_mode_auto_skips_letter_below_threshold(fake_llm, setup_user, engine):
    """docs/05 §6 п.5: score < порога → письмо не генерируется."""
    user_id, vacancy_id = await setup_user(match_threshold=90)
    fake_llm["responses"] = [GOOD_ANALYSIS]  # score 82 < 90
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)

    async with factory() as session:
        outcome = await process_vacancy(
            session, user_id=user_id, vacancy_id=vacancy_id, mode="auto"
        )

    assert outcome.analyzed is True
    assert outcome.letter_generated is False
    assert "порога 90" in (outcome.skipped_reason or "")
    assert outcome.status == "analyzed"
    assert len(fake_llm["calls"]) == 1  # письмо не запрашивалось

    async with factory() as session:
        letter = await session.scalar(select(CoverLetter).where(CoverLetter.vacancy_id == vacancy_id))
    assert letter is None


async def test_mode_auto_uses_threshold_from_request(fake_llm, setup_user, engine):
    """docs/05 §6 п.3: порог из запроса переопределяет порог профиля."""
    user_id, vacancy_id = await setup_user(match_threshold=95)
    fake_llm["responses"] = [GOOD_ANALYSIS, "Здравствуйте! " * 30]
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)

    async with factory() as session:
        outcome = await process_vacancy(
            session, user_id=user_id, vacancy_id=vacancy_id, mode="auto", match_threshold=50
        )

    assert outcome.letter_generated is True
async def test_mode_analyze_and_letter_runs_both_stages(fake_llm, setup_user, engine):
    user_id, vacancy_id = await setup_user()
    fake_llm["responses"] = [GOOD_ANALYSIS, "Здравствуйте! " * 30]
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)

    async with factory() as session:
        outcome = await process_vacancy(
            session, user_id=user_id, vacancy_id=vacancy_id, mode="analyze_and_letter"
        )

    assert outcome.analyzed and outcome.letter_generated
    assert outcome.status == "letter_ready"


async def test_letter_regeneration_bumps_version(fake_llm, setup_user, engine):
    """docs/05 §9: повторная генерация → новая версия письма."""
    user_id, vacancy_id = await setup_user()
    letter_text = "Здравствуйте! " * 30
    fake_llm["default"] = letter_text
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)

    async with factory() as session:
        first = await process_vacancy(
            session, user_id=user_id, vacancy_id=vacancy_id, mode="letter"
        )
    assert first.letter_version == 1

    async with factory() as session:
        second = await process_vacancy(
            session, user_id=user_id, vacancy_id=vacancy_id, mode="letter"
        )
    assert second.letter_version == 2

    async with factory() as session:
        letters = list(
            (
                await session.scalars(
                    select(CoverLetter).where(CoverLetter.vacancy_id == vacancy_id)
                )
            ).all()
        )
    assert len(letters) == 1  # одна запись на вакансию (UNIQUE), версия обновлена
    assert letters[0].version == 2


async def test_processing_requires_compact_resume(fake_llm, setup_user, engine):
    """Нет compact_resume → понятная ошибка, а не молчаливая пустая запись."""
    user_id, vacancy_id = await setup_user(compact="")
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)

    async with factory() as session:
        with pytest.raises(LLMError, match="compact_resume"):
            await process_vacancy(
                session, user_id=user_id, vacancy_id=vacancy_id, mode="auto"
            )


def test_task_types_map_to_modes():
    """docs/05 §2: типы задач очереди соответствуют режимам обработки."""
    assert MODE_BY_TASK_TYPE["analyze"] == "analyze"
    assert MODE_BY_TASK_TYPE["generate_letter"] == "letter"
    assert MODE_BY_TASK_TYPE["auto_full"] == "auto"


# --- docs/04 §6: LLM-очередь воркера ------------------------------------------

async def test_worker_runs_llm_task_and_completes(fake_llm, setup_user, engine, queue_runner):
    """LLM-job очереди ARQ исполняется и переводит задачу в completed."""
    user_id, vacancy_id = await setup_user()
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)

    async with factory() as session:
        task = Task(
            user_id=user_id,
            task_type="analyze",
            status="pending",
            payload={"vacancy_ids": [str(vacancy_id)], "mode": "analyze"},
        )
        session.add(task)
        await session.commit()
        task_id = task.id

    await queue_runner.run_all_pending(engine)

    async with factory() as session:
        done = await session.get(Task, task_id)

    assert done.status == "completed"
    assert done.result["analyzed"] == 1
    assert done.result["outcomes"][0]["match_score"] == 82
    assert done.finished_at is not None
    # started_at проставляется в обработчике job'ы (docs/02 §3.6).
    assert done.started_at is not None


async def test_worker_llm_slot_is_strictly_one(fake_llm, setup_user, engine, queue_runner):
    """docs/04 §6: LLM-очередь обслуживает не больше MAX_CONCURRENT_LLM_WORKERS.

    Один глобальный слот Redis не даёт запустить вторую LLM-задачу, даже если
    job'ы стартуют одновременно: лишние получают Retry и ждут своей очереди.
    """
    from arq.worker import Retry

    user_id, vacancy_id = await setup_user()
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)

    task_ids: list = []
    async with factory() as session:
        for _ in range(3):
            task = Task(
                user_id=user_id,
                task_type="analyze",
                status="pending",
                payload={"vacancy_ids": [str(vacancy_id)], "mode": "analyze"},
            )
            session.add(task)
            await session.flush()
            task_ids.append(task.id)
        await session.commit()

    assert MAX_CONCURRENT_LLM_WORKERS == 1

    ctx = queue_runner._ctx()
    results = await asyncio.gather(
        *(run_llm_task(ctx, str(task_id)) for task_id in task_ids),
        return_exceptions=True,
    )

    # Только одна задача выполнилась, две остались в очереди (Retry).
    deferred = sum(isinstance(r, Retry) for r in results)
    assert deferred == 2
    assert sum(isinstance(r, dict) for r in results) == 1

    async with factory() as session:
        processing = list(
            (
                await session.scalars(
                    select(Task).where(
                        Task.status == "processing", Task.task_type.in_(LLM_TASK_TYPES)
                    )
                )
            ).all()
        )
    # Ни одна задача не «залипла» в processing после завершения job'ы.
    assert len(processing) == 0


async def test_worker_llm_slot_is_released_after_job(
    fake_llm, setup_user, engine, queue_runner
):
    """Слот LLM освобождается, поэтому следующая задача тоже выполняется."""
    user_id, vacancy_id = await setup_user()
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)

    async with factory() as session:
        task = Task(
            user_id=user_id,
            task_type="analyze",
            status="pending",
            payload={"vacancy_ids": [str(vacancy_id)], "mode": "analyze"},
        )
        session.add(task)
        await session.commit()
        task_id = task.id

    ctx = queue_runner._ctx()
    await run_llm_task(ctx, str(task_id))
    # Второй запуск того же контекста: слот уже свободен.
    await run_llm_task(ctx, str(task_id))

    assert await queue_runner.slots.in_use(LLM_SLOT_GROUP) == 0

    async with factory() as session:
        done = await session.get(Task, task_id)
    assert done.status == "completed"


async def test_worker_marks_llm_task_failed_on_llm_error(monkeypatch, setup_user, engine, queue_runner):
    """docs/05 §7: недоступная модель → задача failed с понятным сообщением."""

    async def _broken(prompt: str, *, temperature: float, timeout=None) -> str:
        raise LLMError("Ollama недоступна: connection refused")

    monkeypatch.setattr(llm_module, "_generate", _broken)

    user_id, vacancy_id = await setup_user()
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)

    async with factory() as session:
        task = Task(
            user_id=user_id,
            task_type="analyze",
            status="pending",
            payload={"vacancy_ids": [str(vacancy_id)], "mode": "analyze"},
        )
        session.add(task)
        await session.commit()
        task_id = task.id

    await queue_runner.run_all_pending(engine)

    async with factory() as session:
        done = await session.get(Task, task_id)

    assert done.status == "failed"
    assert done.error_message