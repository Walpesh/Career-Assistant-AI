"""Прямая проверка LLM-пайплайна анализа и писем на живой модели Ollama.

Запуск: python tools/llm_direct_check.py
Использует реальные данные из БД (вакансия + compact_resume пользователя),
без обращения к HTTP-API — только модули app.modules.analysis_letter.llm.
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass

from sqlalchemy import select  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.db.models import UserProfile, Vacancy  # noqa: E402
from app.db.session import AsyncSessionLocal  # noqa: E402
from app.modules.analysis_letter.llm import (  # noqa: E402
    LLMError,
    analyze_vacancy,
    generate_cover_letter,
)


async def main() -> int:
    print(f"Модель: {settings.llm_model} @ {settings.ollama_base_url}\n")

    async with AsyncSessionLocal() as db:
        profile = await db.scalar(
            select(UserProfile).where(UserProfile.compact_resume.isnot(None)).limit(1)
        )
        if profile is None or not (profile.compact_resume or "").strip():
            print("[ABORT] Нет пользователя с compact_resume — выполните convert-resume.")
            return 2
        vacancy = await db.scalar(
            select(Vacancy).where(
                Vacancy.user_id == profile.user_id,
                Vacancy.description_raw.isnot(None),
            ).limit(1)
        )
        if vacancy is None:
            vacancy = await db.scalar(select(Vacancy).limit(1))
        compact = profile.compact_resume or ""

    fields = {
        "title": vacancy.title,
        "company_name": vacancy.company_name,
        "salary_from": vacancy.salary_from,
        "salary_to": vacancy.salary_to,
        "experience": vacancy.experience,
        "employment_form": vacancy.employment_form,
        "work_format": vacancy.work_format,
        "description_raw": vacancy.description_raw,
    }
    print(f"Вакансия: {vacancy.title} @ {vacancy.company_name}")
    print(f"compact_resume: {len(compact)} симв.\n")

    print("--- Этап 1: анализ вакансии (docs/05 §4) ---")
    t0 = time.time()
    try:
        result = await analyze_vacancy(compact, fields)
    except LLMError as exc:
        print(f"[FAIL] анализ: {exc}")
        return 1
    print(f"  время: {time.time() - t0:.1f} с")
    print(f"  match_score: {result.match_score}")
    print(f"  strengths: {result.strengths}")
    print(f"  weaknesses: {result.weaknesses}")
    print(f"  summary: {result.summary}")
    print(f"  match_details: {result.match_details}")

    print("\n--- Этап 2: сопроводительное письмо (docs/05 §5) ---")
    t0 = time.time()
    try:
        letter = await generate_cover_letter(compact, fields)
    except LLMError as exc:
        print(f"[FAIL] письмо: {exc}")
        return 1
    print(f"  время: {time.time() - t0:.1f} с")
    print(f"  длина: {len(letter)} симв. (цель 1200–1800)")
    print(f"  текст:\n{letter}")

    ok = 0 <= result.match_score <= 100 and 1200 <= len(letter) <= 1800
    print(f"\nИТОГ: {'OK' if ok else 'ПРОВАЛЕНО'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))