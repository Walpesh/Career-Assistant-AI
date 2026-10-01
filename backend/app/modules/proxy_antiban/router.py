"""Proxy & Anti-Ban Module — внутренний модуль без HTTP-эндпоинтов.

Ответственность (docs/04_PARSING_RULES.md §3):
- только резидентные прокси, sticky-сессии 5–10 минут, смена после 60–80 запросов;
- немедленная ротация IP при 429 / капче / серии 404;
- ротация User-Agent, полный набор браузерных заголовков, прогрев сессии;
- случайные задержки 4–8 секунд, human-mimicry для Playwright
  (кривые Безье, случайный скролл, паузы «чтения»);
- интерфейс для Parsing Orchestrator: «получить успешный HTML/JSON или ошибку».

HTTP-роутера у модуля нет (используется другими модулями напрямую).
"""

from fastapi import APIRouter

# Пустой роутер: сохранён для единообразия структуры модулей Modular-Flow.
router = APIRouter(prefix="/proxy", tags=["proxy-antiban"], include_in_schema=False)
