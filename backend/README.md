# Backend — Career-Assistant-AI (FastAPI, Modular-Flow)

Реализация по [docs/01_ARCHITECTURE.md](../docs/01_ARCHITECTURE.md): все модули
Modular-Flow доведены до рабочего состояния — auth с OTP-подтверждением email,
профиль с LLM-сокращением резюме, парсинг в 3 режимах, очередь Redis + ARQ,
LLM-анализ и письма, реал-тайм через WebSocket, privacy и billing.
Эндпоинты — по контракту [docs/03_API_CONTRACTS.md](../docs/03_API_CONTRACTS.md).

## Структура

```
backend/
├── app/
│   ├── main.py                 # FastAPI: middleware (CSP, request_id, metrics), /health*, статика frontend/
│   ├── core/                   # Общая инфраструктура
│   │   ├── config.py           # Настройки (pydantic-settings, .env)
│   │   ├── errors.py           # Единый формат ошибок (error_code)
│   │   ├── logging.py          # structlog JSON + санитизация PII
│   │   ├── mail.py             # SMTP-отправка (OTP-коды)
│   │   ├── rate_limit.py       # Rate limiting (Redis)
│   │   ├── redis_client.py     # Пул Redis
│   │   ├── request_id.py       # X-Request-ID middleware
│   │   ├── security_headers.py # CSP/HSTS и другие security-заголовки
│   │   └── sentry.py           # Sentry (scrub_event, без PII)
│   ├── db/                     # Слой доступа к данным (SQLAlchemy 2.0 async)
│   │   ├── base.py             # DeclarativeBase + TimestampMixin (created_at/updated_at)
│   │   ├── models.py           # 12 ORM-моделей по docs/02_DATABASE.md
│   │   └── session.py          # async engine (asyncpg), AsyncSessionLocal, dependency get_db
│   ├── api/v1/router.py        # Сборка роутеров всех модулей под /api/v1
│   └── modules/                # 12 модулей Modular-Flow
│       ├── auth/               # Регистрация, OTP, логин, JWT          → /auth
│       ├── user_profile/       # Профиль, резюме, порог матчинга        → /profile
│       ├── parsing/            # 3 режима сбора вакансий                → /parsing
│       ├── anti_ban/           # Прокси, fingerprints, антибан          (внутренний)
│       ├── vacancy_storage/    # Хранение, дедупликация, статусы        → /vacancies
│       ├── queue_manager/      # Очереди Redis + ARQ (parsing/llm)       → /tasks
│       ├── analysis_letter/    # Анализ, матчинг, письма                → /analysis, /letters
│       ├── realtime/           # WebSocket-доставка событий             → /ws
│       ├── privacy/            # Выгрузка и удаление ПД                 → /account
│       ├── billing/            # Тарифы, квоты, вебхуки                 → /billing
│       ├── health/             # Readiness-проверки зависимостей        → /health/ready
│       ├── metrics/            # Prometheus-метрики и алерты            → /metrics*
│       └── usage_logger.py     # Учёт расхода квот и прокси-трафика
├── alembic.ini                 # Конфигурация Alembic (async, URL из .env)
├── alembic/                    # Миграции: env.py (async) + versions/
├── tests/                      # Интеграционные тесты (pytest + pytest-asyncio)
├── tools/                      # live_smoke_test, llm_direct_check, filter_check, csp_check, queue_flow_check
├── pyproject.toml              # Зависимости + ruff/mypy/pytest/coverage
├── requirements.lock           # Запиненные версии (production/Docker/CI)
├── worker_entry.py             # Standalone ARQ-воркеры (parsing|llm)
└── .env.example
```

## База данных и миграции (docs/02_DATABASE.md)

PostgreSQL 16+, подключение задаётся `DATABASE_URL` в `backend/.env`
(по умолчанию `postgresql+asyncpg://career:career@localhost:5432/career_assistant`).

```powershell
# первичная генерация схемы (head: ревизия b2c3d4e5f6a7)
python -m alembic upgrade head
# новая миграция после изменений моделей app/db/models.py
python -m alembic revision --autogenerate -m "describe change"
python -m alembic upgrade head
```

Схема включает 12 таблиц: `users`, `user_profiles`, `email_otps`, `vacancies`,
`analyses`, `cover_letters`, `tasks`, `refresh_tokens`, `subscriptions`,
`usage_counters`, `payment_events`, `proxy_usage_logs` — с составным уникальным
индексом `(user_id, hh_vacancy_id)`, CHECK-ограничениями статусов/баллов и
индексами очереди задач (включая частичный `status = 'pending'`).
Подробно — docs/02_DATABASE.md §3.1–§3.12.


## Запуск

```powershell
cd backend
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.lock
copy .env.example .env   # при необходимости отредактируйте
uvicorn app.main:app --reload --port 8000
```

- `http://localhost:8000/health` — проверка живости.
- `http://localhost:8000/health/live` — liveness (Kubernetes).
- `http://localhost:8000/health/ready` — readiness (Postgres/Redis/Ollama).
- `http://localhost:8000/api/v1/...` — REST по контракту.
- `http://localhost:8000/` — отдача собранного frontend (`../frontend`, если каталог существует).

## Production (Docker)

```powershell
# Требуется: JWT_SECRET (>=32 символов) в окружении
$env:JWT_SECRET="..."
docker compose config          # проверка сборки контейнеров
docker compose up --build -d   # postgres:16, redis:7, api, worker-parsing ×N,
                               # worker-llm ×1, ollama, frontend (nginx)
docker compose up --scale worker-parsing=3 -d  # масштабирование парсинга
# worker-llm НЕ масштабировать: строго 1 реплика (1-concurrent-worker limit)
```

## Правила модулей (docs/01 §5)

- Модули не лезут в чужие таблицы — только через публичные интерфейсы модуля.
- Долгие операции (парсинг, LLM) — обязательно через Queue Manager.
- LLM-запросы строго последовательно (один воркер).
- Парсинг — максимум 2 воркера на пользователя.
- Изменения статусов/прогресса публикуются в Realtime Module.

## Тесты

```powershell
cd backend
python -m pytest                      # интеграционные тесты (БД career_assistant_test)
python -m pytest tests/test_billing.py -k quota   # по одному модулю
python tools/live_smoke_test.py       # сквозной HTTP-прогон (нужны backend + Ollama)
python tools/llm_direct_check.py      # прямой прогон LLM-этапов
python tools/filter_check.py          # проверка фильтров парсинга без сети/БД
python tools/queue_flow_check.py      # потоки очереди
python tools/csp_check.py             # проверка CSP-заголовков
```
