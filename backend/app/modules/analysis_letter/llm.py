"""Analysis & Letter Module — LLM: анализ вакансии и генерация письма.

Два этапа из docs/05_LLM_PIPELINE.md:
    §4 «Этап 1. Анализ вакансии + Матчинг»        → analyze_vacancy()
    §5 «Этап 2. Генерация сопроводительного письма» → generate_cover_letter()

Движок — Ollama (`/api/generate`, stream=false); промпты взяты дословно из
docs/05 §4 и §5, параметры — из §8 (temperature 0.35 для анализа, 0.55 для
писем, top_p 0.9, num_ctx 8192, repeat_penalty 1.1).

Обработка ошибок — docs/05 §7:
    невалидный JSON → повторный запрос до 2 раз с жёстким промптом «верни только JSON»;
    таймаут/недоступность → LLMError (задача переводится в failed).

Все вызовы строго последовательные (docs/05 §1): единственный LLM-воркер в
Queue Manager гарантирует отсутствие параллельных запросов к модели.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.core.config import settings

__all__ = [
    "LLMError",
    "AnalysisResult",
    "analyze_vacancy",
    "generate_cover_letter",
    "append_resume_addition",
    "build_preferences_block",
    "ANALYSIS_PROMPT_TEMPLATE",
    "LETTER_PROMPT_TEMPLATE",
    "MAX_JSON_ATTEMPTS",
    "MAX_PREFERENCES_CHARS",
    "MAX_RESUME_ADDITION_CHARS",
]

#: docs/05 §7: невалидный JSON → повторный запрос до 2 раз (всего 3 попытки).
MAX_JSON_ATTEMPTS = 3

#: docs/05 §7: «Слишком длинный контекст → усечённое описание вакансии».
MAX_DESCRIPTION_CHARS = 4000

#: docs/05 §4: сколько символов пожеланий кандидата уходит в промпт анализа.
MAX_PREFERENCES_CHARS = 2000

#: docs/05 §5: лимит дописываемого в конец письма текста (resume_addition).
MAX_RESUME_ADDITION_CHARS = 2000

#: docs/05 §5: требуемый объём сопроводительного письма, символы.
LETTER_MIN_CHARS = 1200
LETTER_MAX_CHARS = 1800


class LLMError(Exception):
    """Ollama недоступна, таймаут, пустой ответ или невалидный JSON."""


# --------------------------------------------------------------------- промпты
# docs/05 §4 — промпт анализа вакансии (дословно).
ANALYSIS_PROMPT_TEMPLATE = """Ты — строгий и объективный HR-эксперт с 15-летним опытом подбора IT и digital-специалистов.

Проанализируй, насколько кандидат соответствует вакансии.

Данные кандидата (compact resume):
{compact_resume}

{preferences_block}Данные вакансии:
Название: {title}
Компания: {company_name}
Опыт: {experience}
Формат работы: {work_format}
Занятость: {employment_form}
Зарплата: {salary}
Описание:
{description_raw}

Задача:
1. Оцени соответствие по шкале от 0 до 100.
2. Выдели сильные стороны кандидата именно под эту вакансию.
3. Выдели слабые стороны и риски.
4. Дай краткий итоговый вывод (2–3 предложения).

Ответ строго в JSON-формате:
{{
  "match_score": 0-100,
  "strengths": ["...", "..."],
  "weaknesses": ["...", "..."],
  "summary": "..."
}}"""

# Жёсткое дополнение при повторной попытке (docs/05 §7).
_JSON_RETRY_SUFFIX = """

ВНИМАНИЕ: предыдущий ответ не удалось разобрать как JSON.
Верни ТОЛЬКО валидный JSON-объект без markdown, без пояснений и без ```.
Формат: {"match_score": <число 0-100>, "strengths": ["пункт"], "weaknesses": ["пункт"], "summary": "текст"}"""

# docs/05 §5 — промпт генерации письма (дословно).
LETTER_PROMPT_TEMPLATE = """Ты — профессиональный карьерный консультант, который пишет сильные, живые и убедительные сопроводительные письма.

Напиши сопроводительное письмо от кандидата на вакансию.

Данные кандидата:
{compact_resume}

Вакансия:
Название: {title}
Компания: {company_name}
Описание:
{description_raw}

Требования к письму:
- Объём: 1200–1800 символов
- Структура: приветствие → почему я подхожу (с опорой на опыт) → что могу дать компании → призыв к действию
- Тон: уверенный, профессиональный, без канцелярита и воды
- Не используй шаблонные фразы («Буду рад возможности», «Рассмотрите мою кандидатуру» и т.д.)
- Письмо должно быть готово к копированию и отправке
- Язык: русский

Верни только текст письма, без пояснений и заголовков."""


# --------------------------------------------------------------------- результаты
@dataclass
class AnalysisResult:
    """Нормализованный результат этапа 1 (docs/05 §4)."""

    match_score: int
    strengths: list[str] = field(default_factory=list)
    weaknesses: list[str] = field(default_factory=list)
    summary: str = ""
    match_details: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "match_score": self.match_score,
            "strengths": self.strengths,
            "weaknesses": self.weaknesses,
            "summary": self.summary,
            "match_details": self.match_details,
        }
# --------------------------------------------------------------------- транспорт
async def _generate(
    prompt: str,
    *,
    temperature: float,
    timeout: float | None = None,
) -> str:
    """Один запрос к Ollama `/api/generate`; пустой ответ → LLMError."""
    payload = {
        "model": settings.llm_model,
        "prompt": prompt,
        "stream": False,
        "options": {
            # docs/05 §8: 0.35 — анализ (детерминированнее), 0.55 — письма.
            "temperature": temperature,
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
    return text


def _vacancy_block(vacancy_fields: dict[str, Any]) -> dict[str, str]:
    """Подстановка данных вакансии в промпт (docs/05 §4)."""

    def value(key: str) -> str:
        val = vacancy_fields.get(key)
        return str(val).strip() if val not in (None, "") else "не указано"

    salary = "не указана"
    salary_from = vacancy_fields.get("salary_from")
    salary_to = vacancy_fields.get("salary_to")
    if salary_from or salary_to:
        salary = f"от {salary_from or '—'} до {salary_to or '—'}"

    description = str(vacancy_fields.get("description_raw") or "").strip()
    if len(description) > MAX_DESCRIPTION_CHARS:
        description = description[:MAX_DESCRIPTION_CHARS]
    return {
        "title": value("title"),
        "company_name": value("company_name"),
        "experience": value("experience"),
        "work_format": value("work_format"),
        "employment_form": value("employment_form"),
        "salary": salary,
        "description_raw": description or "не указано",
    }


def build_preferences_block(preferences: Any) -> str:
    """Блок «Предпочтения кандидата» для промпта анализа (docs/05 §4).

    Поле `user_profiles.analysis_preferences` — свободный текст на человеческом
    языке («не хочу трудоустройство по ТК РФ»). Он попадает в промпт без
    изменений: модель сама решает, противоречит ли вакансия пожеланиям.
    Пустое поле → пустой блок, промпт выглядит как раньше.
    """
    text = str(preferences or "").strip()
    if not text:
        return ""
    if len(text) > MAX_PREFERENCES_CHARS:
        text = text[:MAX_PREFERENCES_CHARS]
    return (
        "Предпочтения кандидата (учитывай их при оценке: если вакансия им "
        "противоречит — отрази это в weaknesses и снизи match_score):\n"
        f"{text}\n\n"
    )


def _extract_json(text: str) -> dict[str, Any]:
    """Достать JSON-объект из ответа модели (терпимо к ```-обёрткам)."""
    cleaned = text.strip()
    fenced = re.search(r"```(?:json)?\s*(.+?)\s*```", cleaned, re.DOTALL)
    if fenced:
        cleaned = fenced.group(1).strip()
    try:
        data = json.loads(cleaned)
    except ValueError:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start == -1 or end <= start:
            raise LLMError(f"В ответе модели нет JSON-объекта: {text[:200]}")
        try:
            data = json.loads(cleaned[start : end + 1])
        except ValueError as exc:
            raise LLMError(f"Невалидный JSON от модели: {exc}") from exc
    if not isinstance(data, dict):
        raise LLMError("Ожидался JSON-объект, получен другой тип")
    return data


def _to_list(value: Any) -> list[str]:
    """Список строк из массива/строки модели (docs/05 §4 — массивы)."""
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        parts = re.split(r"[\n;•]+", value)
        return [part.strip(" -–—•\t") for part in parts if part.strip(" -–—•\t")]
    return []


def _clamp_score(value: Any) -> int:
    """match_score в диапазоне 0..100 (CHECK-ограничение БД, docs/02 §3.4)."""
    try:
        score = int(round(float(value)))
    except (TypeError, ValueError) as exc:
        raise LLMError(f"Модель вернула нечисловой match_score: {value!r}") from exc
    return max(0, min(100, score))
# --------------------------------------------------------------------- этап 1
async def analyze_vacancy(
    compact_resume: str,
    vacancy_fields: dict[str, Any],
    *,
    timeout: float | None = None,
) -> AnalysisResult:
    """Анализ вакансии + матчинг (docs/05 §4) с повторами при невалидном JSON."""
    if not compact_resume.strip():
        raise LLMError("Пустое compact_resume — сначала выполните сокращение резюме")

    block = _vacancy_block(vacancy_fields)
    base_prompt = ANALYSIS_PROMPT_TEMPLATE.format(
        compact_resume=compact_resume.strip(),
        preferences_block=build_preferences_block(
            vacancy_fields.get("analysis_preferences")
        ),
        **block,
    )

    last_error: Exception | None = None
    for attempt in range(1, MAX_JSON_ATTEMPTS + 1):
        prompt = base_prompt if attempt == 1 else base_prompt + _JSON_RETRY_SUFFIX
        try:
            raw_text = await _generate(prompt, temperature=0.35, timeout=timeout)
            data = _extract_json(raw_text)
            score = _clamp_score(data.get("match_score"))
            strengths = _to_list(data.get("strengths"))
            weaknesses = _to_list(data.get("weaknesses"))
            summary = str(data.get("summary") or "").strip()
            if not summary and not strengths and not weaknesses:
                raise LLMError("Модель вернула пустой анализ")
            return AnalysisResult(
                match_score=score,
                strengths=strengths,
                weaknesses=weaknesses,
                summary=summary,
                match_details={
                    "model": settings.llm_model,
                    "attempts": attempt,
                    "required_experience": vacancy_fields.get("experience"),
                    "work_format": vacancy_fields.get("work_format"),
                    "salary_from": vacancy_fields.get("salary_from"),
                    "salary_to": vacancy_fields.get("salary_to"),
                },
                raw={"response": raw_text, "parsed": data},
            )
        except LLMError as exc:
            last_error = exc

    raise LLMError(
        f"Модель {settings.llm_model} не вернула валидный JSON за {MAX_JSON_ATTEMPTS} попытки: {last_error}"
    )


# --------------------------------------------------------------------- этап 2
_FENCE_RE = re.compile(r"^```[a-zA-Z]*\s*|\s*```$")


def _clean_letter(text: str) -> str:
    """Убрать markdown-обёртку и служебные хвосты модели."""
    cleaned = _FENCE_RE.sub("", text.strip()).strip()
    for marker in ("Конец письма", "---", "С уважением,"):
        idx = cleaned.find(marker)
        if idx > 0 and len(cleaned) - idx < 400:
            cleaned = cleaned[:idx].rstrip()
    return re.sub(r"\n{3,}", "\n\n", cleaned)


async def generate_cover_letter(
    compact_resume: str,
    vacancy_fields: dict[str, Any],
    *,
    timeout: float | None = None,
    min_chars: int = LETTER_MIN_CHARS,
    max_chars: int = LETTER_MAX_CHARS,
) -> str:
    """Сопроводительное письмо (docs/05 §5). Текст готов к отправке.

    Если модель не попала в требуемый объём (docs/05 §5: 1200–1800 символов),
    делается один уточняющий запрос — по аналогии с повтором при невалидном
    JSON (docs/05 §7). Критично короткий результат (< 200 символов) — ошибка.
    """
    if not compact_resume.strip():
        raise LLMError("Пустое compact_resume — письмо без данных кандидата не имеет смысла")

    block = _vacancy_block(vacancy_fields)
    prompt = LETTER_PROMPT_TEMPLATE.format(compact_resume=compact_resume.strip(), **block)

    raw_text = await _generate(prompt, temperature=0.55, timeout=timeout)
    letter = _clean_letter(raw_text)
    if len(letter) < LETTER_MIN_CHARS:
        # Уточняющий запрос: просим модель дотянуть объём до требуемого.
        retry_prompt = (
            f"{prompt}\n\nПредыдущий вариант оказался короче {min_chars} символов. "
            f"Напиши полноценное письмо объёмом {min_chars}–{max_chars} символов: "
            "добавь конкретику об опыте кандидата и о том, что он даст компании."
        )
        retry_raw = await _generate(retry_prompt, temperature=0.55, timeout=timeout)
        retry_letter = _clean_letter(retry_raw)
        if len(retry_letter) > len(letter):
            letter = retry_letter

    if len(letter) < 200:
        raise LLMError(f"Модель вернула слишком короткое письмо ({len(letter)} символов)")
    return letter


# ------------------------------------------------------- дописывание «красной строки»
def append_resume_addition(
    letter: str,
    addition: Any,
    *,
    max_chars: int = MAX_RESUME_ADDITION_CHARS,
) -> str:
    """Дописать текст из профиля в конец письма «с красной строки».

    Это скриптовый метод: LLM здесь не участвует, текст добавляется дословно.
    Используется в самом конце обработки вакансии (docs/05 §5, §6): если поле
    «Хотите добавить информацию в конец резюме?» непустое, текст вставляется
    после письма, отделённый пустой строкой. Пустое поле → письмо без изменений.
    """
    extra = str(addition or "").strip()
    if not extra:
        return letter

    body = str(letter or "").rstrip()
    if len(extra) > max_chars:
        extra = extra[:max_chars].rstrip()

    # Не дублируем, если пользователь добавил этот текст повторно
    # (генерация письма может быть запущена несколько раз).
    if body.endswith(extra):
        return body

    return f"{body}\n\n{extra}" if body else extra