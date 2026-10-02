"""User Profile Module — LLM: сокращение резюме (docs/05_LLM_PIPELINE.md §3).

«Этап 0. Конвертация резюме» — обязательный метод: создаётся компактная версия
резюме, чтобы не раздувать контекст модели. Промпт — дословно из docs/05 §3
(лимит объёма берётся из настройки COMPACT_RESUME_MAX_CHARS; по умолчанию 2000
символов согласно TASK, docs/05 рекомендует 1800–2200).

Движок: Ollama (`/api/generate`, stream=false), температура — из docs/05 §8.
Сейчас вызов синхронный (в рамках запроса); при подключении Queue Manager
он будет выполняться в LLM Worker (docs/01 §4.3, строго один поток).
"""

from __future__ import annotations

import httpx

from app.core.config import settings

__all__ = ["LLMError", "compress_resume_text", "trim_to_limit", "COMPRESS_PROMPT_TEMPLATE"]


class LLMError(Exception):
    """Ollama недоступна, таймаут или пустой ответ модели."""


# docs/05_LLM_PIPELINE.md §3 (system + user prompt), лимит — параметром.
COMPRESS_PROMPT_TEMPLATE = """Ты — опытный HR-ассистент и карьерный консультант.
Твоя задача — максимально сжать резюме кандидата, сохранив только ключевую информацию, важную для оценки соответствия вакансиям.

Правила:
- Убери воду, общие фразы и повторы
- Оставь: опыт работы (годы + ключевые роли), основные навыки, образование, достижения с цифрами, стек технологий
- Сохрани профессиональный тон
- Объём результата — не более {max_chars} символов
- Язык ответа — русский

Резюме кандидата:
{resume_text}"""


def trim_to_limit(text: str, limit: int) -> str:
    """Обрезать текст до limit символов, не разрывая слово/предложение.

    Используется и в LLM-ответе, и при записи в БД (единая точка обрезки),
    поэтому результат всегда ≤ limit символов, а граница режет по
    ближайшему пробелу или знаку конца предложения.
    """
    value = (text or "").strip()
    if limit <= 0 or len(value) <= limit:
        return value

    cut = value[:limit]
    # Точка/вопрос/восклицание/перевод строки — предпочтительная граница.
    for separator in (". ", ".\n", "\n", "; ", ", "):
        position = cut.rfind(separator)
        if position >= limit // 2:
            return cut[: position + 1].strip()
    position = cut.rfind(" ")
    if position >= limit // 2:
        return cut[:position].strip()
    return cut.strip()


async def compress_resume_text(
    resume_text: str,
    *,
    max_chars: int | None = None,
    timeout: float | None = None,
) -> str:
    """Запрос к Ollama на сокращение резюме; результат обрезается до max_chars.

    Бросает LLMError при недоступности Ollama, таймауте, невалидном ответе
    или пустом результате (docs/05 §7).
    """
    limit = max_chars if max_chars is not None else settings.compact_resume_max_chars
    prompt = COMPRESS_PROMPT_TEMPLATE.format(max_chars=limit, resume_text=resume_text)

    payload = {
        "model": settings.llm_model,
        "prompt": prompt,
        "stream": False,
        "options": {
            "temperature": 0.35,  # docs/05 §8 — сокращение/анализ: детерминированнее
            "top_p": 0.9,
            "num_ctx": 8192,
            "repeat_penalty": 1.1,
        },
    }

    try:
        async with httpx.AsyncClient(
            base_url=settings.ollama_base_url,
            timeout=timeout if timeout is not None else settings.llm_timeout_seconds,
        ) as client:
            response = await client.post("/api/generate", json=payload)
            response.raise_for_status()
            data = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise LLMError(f"Ollama недоступна или вернула невалидный ответ: {exc}") from exc

    text = str(data.get("response") or "").strip()
    if not text:
        raise LLMError("Ollama вернула пустой ответ")

    # Гарантируем лимит из TASK даже при «болтливой» модели.
    return trim_to_limit(text, limit)
