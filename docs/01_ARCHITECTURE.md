# 01_ARCHITECTURE.md
# Архитектура Career-Assistant-AI (Modular-Flow)

## 1. Общий принцип архитектуры

Система построена по модели **Modular-Flow** — независимые модули с чёткими входными/выходными контрактами и единым направленным потоком данных.

Каждый модуль может разрабатываться, тестироваться и масштабироваться относительно независимо. Связь между модулями осуществляется через:
- Прямые вызовы внутри backend (FastAPI)
- Очередь задач (Redis + ARQ/Celery)
- WebSocket для реал-тайм уведомлений

---

## 2. Высокоуровневая схема Modular-Flow
[Frontend]
│
│  REST + WebSocket
▼
[API Gateway — FastAPI]
│
├──► [Auth Module] ──────────────────┐
│                                    │
├──► [User Profile Module] ◄─────────┘
│
├──► [Parsing Orchestrator]
│         │
│         ├──► [Proxy & Anti-Ban Module]
│         │
│         └──► [Vacancy Storage Module]
│
├──► [Queue Manager]
│         │
│         ├── Parsing Workers (до 2 на пользователя)
│         │
│         └── LLM Worker (строго 1 поток)
│
├──► [Analysis & Letter Module]
│
└──► [Realtime & Notification Module] ──► Frontend (WebSocket)
text---

## 3. Описание модулей и их ответственности

| Модуль | Ответственность | Основные входы | Основные выходы |
|--------|------------------|----------------|-----------------|
| **Auth Module** | Регистрация, авторизация, JWT | Логин/пароль | user_id, токены |
| **User Profile Module** | Хранение резюме, настроек, порога матчинга, сокращённой версии резюме | Данные профиля, команда «Конвертировать» | Профиль + compact_resume |
| **Parsing Orchestrator** | Управление тремя режимами сбора вакансий | Запрос на парсинг (авто/группа/ручной) | Список hh_vacancy_id + сырые данные |
| **Proxy & Anti-Ban Module** | Ротация прокси, fingerprints, задержки, human-mimicry, обработка 429/капчи | Запрос на HTTP/браузер | Успешный HTML/JSON или ошибка |
| **Vacancy Storage Module** | Сохранение, дедупликация, обновление статусов | Сырые/очищенные данные вакансии | vacancy_id + текущий статус |
| **Queue Manager** | Единая очередь задач, контроль параллелизма | Новая задача | Статус задачи + прогресс |
| **Analysis & Letter Module** | Анализ, матчинг, генерация писем (4 режима) | vacancy_id + режим | analysis + cover_letter + match_score |
| **Realtime & Notification Module** | Доставка событий на фронтенд | События от всех модулей | WebSocket-сообщения + Fadeout-popup |

---

## 4. Основные потоки данных (Flows)

### 4.1. Flow: Добавление вакансий (Parsing Flow)
1. Frontend → API Gateway (запрос на Автопоиск / Групповой / Ручной)
2. API Gateway → Parsing Orchestrator
3. Parsing Orchestrator → Proxy & Anti-Ban Module (получение страниц)
4. Parsing Orchestrator → Vacancy Storage Module (сохранение + дедупликация)
5. Vacancy Storage → Queue Manager (опционально ставит задачу на анализ)
6. Realtime Module → Frontend (прогресс парсинга)

### 4.2. Flow: Анализ и генерация письма (Analysis Flow)
1. Frontend → API Gateway (выбор режима: Только Анализ / Только Письмо / Анализ+Письмо / AUTO)
2. API Gateway → Queue Manager
3. Queue Manager → LLM Worker (строго последовательно)
4. LLM Worker → Analysis & Letter Module
5. Analysis & Letter Module → Vacancy Storage (сохранение результатов + смена статуса)
6. Realtime Module → Frontend (прогресс + готовый результат)

### 4.3. Flow: Обновление профиля и конвертация резюме
1. Frontend → User Profile Module
2. При нажатии «Конвертировать» → Queue Manager → LLM Worker
3. Результат сохраняется как `compact_resume`

---

## 5. Правила взаимодействия модулей

- Модули **не** обращаются к базе данных напрямую друг друга — только через свои публичные интерфейсы.
- Все долгие операции (парсинг, LLM) обязательно проходят через Queue Manager.
- LLM-запросы **никогда** не выполняются параллельно (один воркер).
- Парсинг может иметь ограниченный параллелизм (максимум 2 воркера на пользователя).
- Любое изменение статуса вакансии или прогресса задачи обязательно публикуется в Realtime Module.

---

## 6. Масштабирование

- Parsing Workers — горизонтально (несколько процессов/контейнеров)
- LLM Worker — вертикально (один мощный процесс) + возможность позже вынести в отдельный сервис
- Redis и PostgreSQL — стандартное масштабирование
- Frontend остаётся лёгким и не требует масштабирования на первом этапе
---

## 7. Observability (логирование, метрики, ошибки)

Ссылка на реализацию: `backend/app/core/logging.py`, `backend/app/core/request_id.py`,
`backend/app/core/sentry.py`, `backend/app/modules/metrics/`.

### 7.1. Структурированное JSON-логирование

- **structlog** с JSON-рендерером (`JSONRenderer`) — одна JSON-строка на событие;
  в development `LOG_JSON_OUTPUT=false` включает читаемый console-вывод.
- Процессоры: `merge_contextvars` → `add_log_level` → `TimeStamper` →
  `add_logger_name` → `format_exc_info` → **санитизация PII** → рендерер.
- Логгеры `uvicorn`/`sqlalchemy.engine` переводятся на тот же JSON-handler,
  поэтому access-логи и логи БД тоже структурированы.

Поля события: `timestamp`, `level`, `logger`, `event`, `request_id`, `exception`.

### 7.2. request_id

- `RequestIDMiddleware` берёт входящий `X-Request-ID` (или генерирует 16 hex-символов),
  кладёт его в `contextvars` и возвращает в ответе тем же заголовком.
- Значение санитизируется: допустимы только `[A-Za-z0-9_-]`, максимум 64 символа —
  клиент не может «отравить» логи заголовком.
- WebSocket-сессии получают свой `request_id` (`resolve_ws_request_id`), доступный
  всем логам Realtime Module.

### 7.3. Правило «никаких PII в логах»

Никогда не логируются и не отправляются в Sentry:

| Категория | Что маскируется |
|---|---|
| Заголовки | `Authorization`, `Proxy-Authorization`, `Cookie`, `Set-Cookie`, `X-Api-Key`, `X-Auth-Token`, `X-CSRF-Token` |
| Секреты | `jwt_secret`, `password`, `access_token`, `refresh_token`, `ws_ticket`, `api_key`, `secret` |
| ПД кандидата | `email`, `phone`, `telegram`, `full_name`, `first_name`/`last_name`, `resume_text`, `compact_resume`, `cover_letter` |
| Свободный текст | JWT/Bearer-токены, email-адреса и телефоны по регулярным выражениям |

Маскировка применяется на любом уровне вложенности payload; технические ключи
(`event`, `status`, `request_id`, `endpoint`, …) не сканируются на PII.

### 7.4. Prometheus-метрики — `GET /metrics`

Text exposition 0.0.4 (`prometheus_client`; при отсутствии пакета включается
встроенный совместимый реестр). Дополнительно: `GET /metrics/summary` (JSON) и
`GET /metrics/alerts` (пороги).

| Метрика | Тип | Label | Назначение |
|---|---|---|---|
| `http_request_duration_seconds` | Histogram | `endpoint`, `method` | Латентность HTTP по endpoint (шаблон маршрута, не сырой путь) |
| `http_requests_total` | Counter | `endpoint`, `method`, `status` | Счётчик запросов |
| `arq_queue_length` | Gauge | `queue` (`parsing`/`llm`) | Длина очередей ARQ |
| `queue_semaphore_slots_active` | Gauge | `group` | Активные слоты Redis-семафора |
| `captcha_encounters_total` | Counter | `detector` | Встреченные капчи hh.ru |
| `fetch_total` | Counter | `result` | Попытки fetch (ok/captcha/error) |
| `llm_execution_duration_seconds` | Histogram | `task_type` | Длительность LLM-вызовов |
| `tasks_completed_total` | Counter | `task_type` | Успешно завершённые задачи |
| `tasks_failed_total` | Counter | `task_type`, `reason` | Отказы задач |
| `task_errors_total` | Counter | `task_type` | Неожиданные ошибки задач |
| `ollama_up` | Gauge | — | Доступность Ollama (1/0) |
| `career_alert_firing` | Gauge | `alert` | Состояние порогов алертов |

Сбор `arq_queue_length`, `queue_semaphore_slots_active` и `ollama_up` выполняет
фоновая задача `app.modules.metrics.collector` (раз в 15 с) и по запросу
`/metrics/alerts`.

### 7.5. Пороги алертов

Пороги задаются переменными окружения и применяются в
`app.modules.metrics.alerts.evaluate_alerts()`; при нарушении пишется
JSON-событие `alert_firing` и отправляется сообщение в Sentry.

| Алерт | Порог (env) | По умолчанию | Условие |
|---|---|---|---|
| `llm_queue_pending` | `ALERT_LLM_QUEUE_PENDING_THRESHOLD` | 10 | pending-задач в `career:queue:llm` > порога |
| `captcha_rate` | `ALERT_CAPTCHA_RATE_THRESHOLD` | 0.05 (5%) | капчи / fetch > порога |
| `ollama_down` | `ALERT_OLLAMA_DOWN_SECONDS` | 60 с | Ollama недоступна дольше порога |
| `task_failure_rate` | `ALERT_TASK_FAILURE_RATE_THRESHOLD` | 0.25 | отказы / завершённые задачи > порога |

### 7.6. Sentry

- Включается только при непустом `SENTRY_DSN`; иначе все точки вызова — no-op.
- `send_default_pii=False` и `request_bodies="never"` — PII не покидает процесс.
- `before_send=scrub_event` дополнительно вычищает `Authorization`/`Cookie`,
  тело и query-строку запроса, `extra`, `contexts`, `tags`, `exception`,
  breadcrumbs, а из `user` оставляет только технический `id`.
- Ошибки 5xx из HTTP-обработчиков и исключения ARQ-задач отправляются
  автоматически; тег `request_id` связывает событие с логами запроса.

### 7.7. Проверка

```bash
curl http://localhost:8000/metrics
curl http://localhost:8000/metrics/summary
curl http://localhost:8000/metrics/alerts
```
