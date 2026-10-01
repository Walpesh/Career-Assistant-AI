# Career-Assistant-AI

Коммерческий веб-сервис автоматизации поиска, парсинга и анализа вакансий hh.ru
(без официального API) с генерацией персонализированных сопроводительных писем через LLM.

Архитектура: **Modular-Flow** — независимые модули с чёткими контрактами и единым потоком данных
(REST + WebSocket между frontend и backend, Redis-очереди для долгих операций).

---

## Документация проекта

| Файл | Содержание |
|------|------------|
| [docs/00_OVERVIEW.md](docs/00_OVERVIEW.md) | Общее описание, стек, KPI |
| [docs/01_ARCHITECTURE.md](docs/01_ARCHITECTURE.md) | Архитектура Modular-Flow, модули, потоки данных |
| [docs/02_DATABASE.md](docs/02_DATABASE.md) | Схема PostgreSQL, таблицы, статусы вакансий |
| [docs/03_API_CONTRACTS.md](docs/03_API_CONTRACTS.md) | REST-контракты, WebSocket-события |
| [docs/04_PARSING_RULES.md](docs/04_PARSING_RULES.md) | Правила парсинга, антибан, лимиты |
| [docs/05_LLM_PIPELINE.md](docs/05_LLM_PIPELINE.md) | Промпты, режимы анализа, генерация писем |

---

## Структура репозитория

```
.
├── backend/     # FastAPI-приложение: app/modules/* (8 модулей Modular-Flow)
├── frontend/    # SPA: HTML5 + Tailwind CSS + Vanilla JS (ES-модули), см. frontend/README.md
├── docs/        # Спецификации проекта
└── tools/       # Служебные скрипты (UI smoke test)
```

---

## Быстрый старт

### Frontend (статическая вёрстка, без сборки)

```powershell
python -m http.server 5500 --directory frontend
```

- `http://localhost:5500/` — интерфейс (ожидает backend на `http://localhost:8000`).
- `http://localhost:5500/?demo=1` — **демо-режим**: фиктивные данные строго по контрактам,
  backend не нужен (для проверки вёрстки и реал-тайм компонентов).
- `http://localhost:5500/?api=http://localhost:8000/api/v1` — явное указание адреса API
  (сохраняется в localStorage).

### Backend (скелет приложения)

```powershell
cd backend
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
uvicorn app.main:app --reload --port 8000
```

Backend монтирует каталог `frontend/` и отдаёт интерфейс по адресу `http://localhost:8000/`
(API — по префиксу `/api/v1`, см. [docs/03_API_CONTRACTS.md](docs/03_API_CONTRACTS.md)).

### UI smoke test (Playwright)

```powershell
pip install playwright
python -m playwright install chromium
python tools/ui_smoke_test.py
```

---

## Статус

- [x] Каталог-структура Modular-Flow (`frontend/`, `backend/app/modules/*`)
- [x] Полный UI: Auth, «Моё резюме» (compact_resume + порог матчинга), Парсинг-дашборд (3 режима),
      «Анализ и Отклик» (статусы, match-теги, массовые действия, письма), реал-тайм журнал и прогресс-бары
- [x] Кастомный компонент `Fadeout-action-popup` (рантайм-уведомления + fallback для WS-события `popup`)
- [ ] Бизнес-логика backend-модулей (сейчас — точки подключения роутеров по контрактам)

