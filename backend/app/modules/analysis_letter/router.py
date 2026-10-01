"""Analysis & Letter Module — точки подключения роутеров (docs/03_API_CONTRACTS.md §6).

Контракт:
    POST /analysis/run            — запуск обработки:
                                     { vacancy_ids[], mode, match_threshold? }
                                     mode: analyze | letter | analyze_and_letter | auto
    GET  /analysis/{vacancy_id}   — получить анализ (match_score, strengths,
                                     weaknesses, summary — docs/02 §3.4)
    GET  /letters/{vacancy_id}    — получить сопроводительное письмо
                                     (content, version — docs/02 §3.5)

Логика режимов и промпты: docs/05_LLM_PIPELINE.md §2–6
(AUTO: анализ → проверка порога → письмо; LLM строго последовательно).
"""

from fastapi import APIRouter

analysis_router = APIRouter(prefix="/analysis", tags=["analysis"])
letters_router = APIRouter(prefix="/letters", tags=["letters"])
