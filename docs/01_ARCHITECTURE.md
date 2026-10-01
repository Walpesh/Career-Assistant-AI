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