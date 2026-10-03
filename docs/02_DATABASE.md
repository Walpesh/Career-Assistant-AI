# 02_DATABASE.md
# Схема базы данных Career-Assistant-AI

## 1. Общие принципы

- СУБД: **PostgreSQL 16+**
- Все таблицы имеют `created_at` и `updated_at` (timestamptz)
- Мягкое удаление не используется (на первом этапе)
- Основной ключ связи пользователя — `user_id` (UUID)
- Уникальность вакансий обеспечивается составным индексом `(user_id, hh_vacancy_id)`

---

## 2. Список таблиц

| Таблица              | Назначение                                      |
|-----------------------|-------------------------------------------------|
| `users`               | Аккаунты пользователей                          |
| `user_profiles`       | Профиль, резюме, настройки матчинга             |
| `vacancies`           | Вакансии, привязанные к пользователю            |
| `analyses`            | Результаты анализа вакансий                     |
| `cover_letters`       | Сгенерированные сопроводительные письма         |
| `tasks`               | Очередь задач (для отображения прогресса)       |
| `refresh_tokens`      | Реестр refresh-токенов (ротация, reuse detection)|
| `subscriptions`       | Тарифный план пользователя (free / pro / enterprise) |
| `usage_counters`      | Расход суточных квот по видам операций           |
| `payment_events`      | Идемпотентность вебхуков платёжных шлюзов       |
| `proxy_usage_logs`    | Объём прокси-трафика и доля капчи по задачам     |

---

## 3. Описание таблиц

### 3.1. `users`

| Поле            | Тип              | Ограничения                  | Описание                     |
|-----------------|------------------|------------------------------|------------------------------|
| id              | UUID             | PRIMARY KEY, DEFAULT gen_random_uuid() | ID пользователя         |
| email           | VARCHAR(255)     | UNIQUE, NOT NULL            | Email                        |
| password_hash   | VARCHAR(255)     | NOT NULL                    | Хэш пароля                   |
| is_active       | BOOLEAN          | DEFAULT true                 | Активен ли аккаунт           |
| created_at      | TIMESTAMPTZ      | DEFAULT now()                | Дата регистрации             |
| updated_at      | TIMESTAMPTZ      | DEFAULT now()                | Дата обновления              |

---

### 3.2. `user_profiles`

| Поле                  | Тип              | Ограничения                  | Описание                                      |
|-----------------------|------------------|------------------------------|-----------------------------------------------|
| user_id               | UUID             | PRIMARY KEY, FK → users.id   | Связь 1:1 с пользователем                     |
| full_name             | VARCHAR(255)     |                              | Имя кандидата                                 |
| resume_text           | TEXT             |                              | Полное резюме (до 5000 символов)              |
| compact_resume        | TEXT             |                              | Сокращённая версия резюме (результат LLM)     |
| skills                | TEXT[]           |                              | Список навыков                                |
| experience_years      | NUMERIC(4,1)     |                              | Опыт в годах                                  |
| desired_salary_from   | INTEGER          |                              | Желаемая зарплата от                          |
| desired_salary_to     | INTEGER          |                              | Желаемая зарплата до                          |
| match_threshold       | SMALLINT         | DEFAULT 70, CHECK (0–100)    | Порог матчинга по умолчанию (%)               |
| preferred_work_formats| TEXT[]           |                              | Предпочитаемые форматы работы                 |
| analysis_preferences  | TEXT             |                              | Пожелания на человеческом языке; влияют на LLM-анализ (docs/05 §4) |
| resume_addition       | TEXT             |                              | Текст, дописываемый «с красной строки» в конец письма (docs/05 §5) |
| created_at            | TIMESTAMPTZ      | DEFAULT now()                |                                               |
| updated_at            | TIMESTAMPTZ      | DEFAULT now()                |                                               |

---

### 3.3. `vacancies`

| Поле              | Тип              | Ограничения                              | Описание                                      |
|-------------------|------------------|------------------------------------------|-----------------------------------------------|
| id                | UUID             | PRIMARY KEY, DEFAULT gen_random_uuid()   | Внутренний ID вакансии                        |
| user_id           | UUID             | NOT NULL, FK → users.id                 | Владелец вакансии                             |
| hh_vacancy_id     | VARCHAR(32)      | NOT NULL                                | ID вакансии на hh.ru                          |
| url               | TEXT             | NOT NULL                                | Полная ссылка на вакансию                     |
| title             | VARCHAR(512)     |                                          | Название вакансии                             |
| company_name      | VARCHAR(512)     |                                          | Название компании                             |
| salary_from       | INTEGER          |                                          | Зарплата от                                   |
| salary_to         | INTEGER          |                                          | Зарплата до                                   |
| salary_currency   | VARCHAR(8)       | DEFAULT 'RUR'                            | Валюта                                        |
| experience        | VARCHAR(64)      |                                          | Требуемый опыт                                |
| employment_form   | VARCHAR(64)      |                                          | Форма трудоустройства                         |
| work_format       | VARCHAR(64)      |                                          | Формат работы (remote/hybrid/onsite и т.д.)   |
| schedule          | VARCHAR(128)     |                                          | График работы                                 |
| area              | VARCHAR(255)     |                                          | Город / регион                                |
| published_at      | TIMESTAMPTZ      |                                          | Дата публикации на hh.ru                      |
| description_raw   | TEXT             |                                          | Сырое описание (очищенное)                    |
| description_html  | TEXT             |                                          | Оригинальный HTML (опционально)               |
| status            | VARCHAR(32)      | NOT NULL, DEFAULT 'raw'                 | raw / analyzed / letter_ready / applied / error |
| match_score       | SMALLINT         | CHECK (0–100)                            | Итоговый балл матчинга                        |
| source            | VARCHAR(32)      |                                          | auto / group / manual                         |
| created_at        | TIMESTAMPTZ      | DEFAULT now()                            |                                               |
| updated_at        | TIMESTAMPTZ      | DEFAULT now()                            |                                               |

**Индексы:**
- UNIQUE (user_id, hh_vacancy_id)
- INDEX (user_id, status)
- INDEX (user_id, created_at DESC)

---

### 3.4. `analyses`

| Поле              | Тип              | Ограничения                              | Описание                                      |
|-------------------|------------------|------------------------------------------|-----------------------------------------------|
| id                | UUID             | PRIMARY KEY                              |                                               |
| vacancy_id        | UUID             | NOT NULL, FK → vacancies.id, UNIQUE     | Одна запись анализа на вакансию               |
| match_score       | SMALLINT         |                                          | Числовая оценка (0–100)                       |
| match_details     | JSONB            |                                          | Детали матчинга (skills overlap, опыт и т.д.) |
| strengths         | TEXT             |                                          | Сильные стороны кандидата под вакансию        |
| weaknesses        | TEXT             |                                          | Слабые стороны / риски                        |
| summary           | TEXT             |                                          | Краткий вывод LLM                             |
| raw_llm_response  | JSONB            |                                          | Полный ответ модели (для отладки)             |
| created_at        | TIMESTAMPTZ      | DEFAULT now()                            |                                               |
| updated_at        | TIMESTAMPTZ      | DEFAULT now()                            |                                               |

---

### 3.5. `cover_letters`

| Поле              | Тип              | Ограничения                              | Описание                                      |
|-------------------|------------------|------------------------------------------|-----------------------------------------------|
| id                | UUID             | PRIMARY KEY                              |                                               |
| vacancy_id        | UUID             | NOT NULL, FK → vacancies.id, UNIQUE     | Одно письмо на вакансию                       |
| content           | TEXT             | NOT NULL                                | Текст сопроводительного письма                |
| version           | INTEGER          | DEFAULT 1                                | Версия письма (при перегенерации)             |
| raw_llm_response  | JSONB            |                                          | Полный ответ модели                           |
| created_at        | TIMESTAMPTZ      | DEFAULT now()                            |                                               |
| updated_at        | TIMESTAMPTZ      | DEFAULT now()                            |                                               |

---

### 3.6. `tasks` (очередь задач)

| Поле              | Тип              | Ограничения                              | Описание                                      |
|-------------------|------------------|------------------------------------------|-----------------------------------------------|
| id                | UUID             | PRIMARY KEY                              |                                               |
| user_id           | UUID             | NOT NULL, FK → users.id                 | Владелец задачи                               |
| task_type         | VARCHAR(64)      | NOT NULL                                | parse_auto / parse_group / parse_manual / analyze / generate_letter / auto_full / convert_resume |
| status            | VARCHAR(32)      | NOT NULL, DEFAULT 'pending'             | pending / processing / completed / failed / waiting_captcha (CHECK ck_tasks_status) |
| progress_current  | INTEGER          | DEFAULT 0                                | Текущий прогресс                              |
| progress_total    | INTEGER          | DEFAULT 0                                | Общее количество шагов                        |
| progress_stage    | VARCHAR(32)      |                                          | Последний этап прогресса (например parsing_vacancy / llm) |
| progress_message  | TEXT             |                                          | Последнее сообщение прогресса — переживает перезагрузку страницы |
| payload           | JSONB            |                                          | Входные параметры задачи                      |
| result            | JSONB            |                                          | Результат выполнения                          |
| error_message     | TEXT             |                                          | Текст ошибки (если failed)                    |
| related_vacancy_id| UUID             | FK → vacancies.id                        | Связанная вакансия (если есть)                |
| created_at        | TIMESTAMPTZ      | DEFAULT now()                            |                                               |
| started_at        | TIMESTAMPTZ      |                                          |                                               |
| finished_at       | TIMESTAMPTZ      |                                          |                                               |

**Индексы:**
- INDEX (user_id, status)
- INDEX (status, created_at) — для воркеров

**Статусы `tasks.status`:**

| Значение         | Когда                                                        |
|------------------|--------------------------------------------------------------|
| `pending`        | Создана и поставлена в очередь Redis                          |
| `processing`     | Воркер начал выполнение (`started_at`)                        |
| `completed`      | Успешно завершена (`finished_at`, `result`)                   |
| `failed`         | Ошибка или отмена пользователем (`error_message`, `finished_at`) |
| `waiting_captcha`| Обнаружена капча hh.ru — пауза под ручное вмешательство (docs/04 §2 п.3, §5); `finished_at` не проставляется, задача возвращается в очередь через `POST /tasks/{id}/resume` |

---

### 3.7. `refresh_tokens` (реестр refresh-токенов)

Серверный учёт выданных refresh-токенов: хранится **только SHA-256 хэш**
(`hashed_token`), сам токен в БД не пишется. Нужен для ротации и обнаружения
reuse (кражи) токена.

| Поле           | Тип              | Ограничения                              | Описание                                |
|----------------|------------------|------------------------------------------|-----------------------------------------|
| id             | UUID             | PRIMARY KEY, DEFAULT gen_random_uuid()   |                                         |
| user_id        | UUID             | NOT NULL, FK → users.id (cascade delete) | Владелец токена                         |
| jti            | VARCHAR(64)      | NOT NULL, UNIQUE                         | ID токена (JWT `jti`)                   |
| hashed_token   | VARCHAR(64)      | NOT NULL, UNIQUE                         | SHA-256 хэш refresh-токена              |
| user_agent     | VARCHAR(255)     |                                          | User-Agent клиента (audit)              |
| expires_at     | TIMESTAMPTZ      | NOT NULL                                 | Срок жизни                              |
| revoked_at     | TIMESTAMPTZ      |                                          | NULL = активен; иначе отозван/ротирован |
| created_at     | TIMESTAMPTZ      | DEFAULT now()                            |                                         |

**Логика:** при `/auth/refresh` токен ротируется (старый → `revoked_at`,
новый создаётся). Повторное предъявление отозванного токена = reuse →
отзываются **все** токены пользователя. `POST /auth/logout` ставит `revoked_at`.

**Индексы:**
- INDEX (user_id)
- UNIQUE (jti)
- UNIQUE (hashed_token)

---

### 3.8. `subscriptions` (тарифный план)

Тариф определяет суточные квоты (Billing Module, docs/03 §11). Отсутствие
строки равносильно тарифу `free`, поэтому «новый» аккаунт никогда не падает
при обращении к тарифу.

| Поле                    | Тип          | Ограничения                                | Описание                                  |
|-------------------------|--------------|--------------------------------------------|-------------------------------------------|
| id                      | UUID         | PRIMARY KEY, DEFAULT gen_random_uuid()     |                                           |
| user_id                 | UUID         | NOT NULL, FK → users.id (cascade delete), UNIQUE | Тариф пользователя (не более одного) |
| tier                    | VARCHAR(16)  | NOT NULL, DEFAULT 'free', CHECK            | free / pro / enterprise                   |
| status                  | VARCHAR(16)  | NOT NULL, DEFAULT 'active', CHECK          | active / past_due / canceled / expired    |
| provider                | VARCHAR(32)  |                                            | yookassa / cloudpayments / stripe         |
| external_id             | VARCHAR(128) |                                            | Идентификатор платежа у шлюза             |
| daily_parsing_jobs      | INTEGER      | CHECK ≥ 0                                  | Переопределение квоты (NULL = по тарифу)  |
| daily_cover_letters     | INTEGER      | CHECK ≥ 0                                  | Переопределение квоты                     |
| daily_analyses          | INTEGER      | CHECK ≥ 0                                  | Переопределение квоты                     |
| daily_proxy_mb          | INTEGER      | CHECK ≥ 0                                  | Переопределение квоты                     |
| current_period_end      | TIMESTAMPTZ  |                                            | Конец оплаченного периода                 |
| canceled_at             | TIMESTAMPTZ  |                                            | Момент отмены подписки                    |
| created_at / updated_at | TIMESTAMPTZ  | DEFAULT now()                              |                                           |

**Индексы:** UNIQUE (user_id) — один тариф на пользователя.

> Значения `daily_*` — индивидуальные переопределения для enterprise-контрактов,
> где лимиты фиксируются письменно, а не по каталогу тарифов.

---

### 3.9. `usage_counters` (расход суточных квот)

Квота считается по календарным суткам **UTC** и по видам операций
(`quota_kind`). Счётчик «обнуляется» сменой `day`, поэтому история не растёт
бесконечно и не требует фоновой очистки в горячем пути.

| Поле                   | Тип          | Ограничения                              | Описание                              |
|------------------------|--------------|------------------------------------------|---------------------------------------|
| id                     | UUID         | PRIMARY KEY                              |                                       |
| user_id                | UUID         | NOT NULL, FK → users.id (cascade delete) | Владелец квоты                        |
| day                    | DATE         | NOT NULL                                 | Сутки расхода (UTC)                   |
| quota_kind             | VARCHAR(16)  | NOT NULL, CHECK                          | parse / letter / analysis / proxy_mb  |
| used                   | INTEGER      | NOT NULL, DEFAULT 0, CHECK ≥ 0           | Израсходовано за сутки                |
| created_at / updated_at| TIMESTAMPTZ  | DEFAULT now()                            |                                       |

**Индексы:**
- UNIQUE (user_id, day, quota_kind) — одновременно путь `ON CONFLICT` для
  атомарного начисления (`INSERT … ON CONFLICT DO UPDATE … WHERE`), поэтому
  параллельные запросы физически не могут превысить лимит;
- INDEX (day) — выборка «расход за сутки» для сверки и отчётности.

---

### 3.10. `payment_events` (идемпотентность вебхуков)

Платёжные шлюзы гарантированно повторяют доставку уведомления при отсутствии
ответа. Ключ `(provider, external_event_id)` гарантирует, что тариф меняется
**один** раз, даже если событие пришло трижды или два запроса пришли
одновременно.

| Поле              | Тип          | Ограничения                          | Описание                            |
|-------------------|--------------|--------------------------------------|-------------------------------------|
| id                | UUID         | PRIMARY KEY                          |                                     |
| provider          | VARCHAR(32)  | NOT NULL                             | Платёжный шлюз                      |
| external_event_id | VARCHAR(128) | NOT NULL                             | ID события у шлюза                  |
| event_type        | VARCHAR(64)  | NOT NULL                             | Нормализованный тип события         |
| user_id           | UUID         |                                      | Ссылка на пользователя (nullable)    |
| tier              | VARCHAR(16)  |                                      | Применённый тариф                   |
| status            | VARCHAR(16)  | NOT NULL, DEFAULT 'processed', CHECK | processed / ignored / failed        |
| payload           | JSONB        |                                      | Тело события (аудит)                |
| created_at        | TIMESTAMPTZ  | DEFAULT now()                        |                                     |

**Индексы:**
- UNIQUE (provider, external_event_id) — **ключ идемпотентности**;
- INDEX (user_id).

> `user_id` здесь nullable и **без внешнего ключа**: при удалении аккаунта
> Privacy Module обезличивает запись (обнуляет ссылку), но сохраняет её.
> Если бы запись удалялась, повторная доставка вебхука после удаления
> аккаунта воскресила бы подписку для несуществующего пользователя.

---

### 3.11. `proxy_usage_logs` (учёт прокси-трафика)

Proxy Usage Logger пишет **одну строку на задачу**: объём скачанного трафика,
число запросов и число встреченных капч. По этим данным считается себестоимость
парсинга и поднимается предупреждение при доле капчи выше 5 % (docs/04 §9).

| Поле           | Тип         | Ограничения                                            | Описание                |
|----------------|-------------|--------------------------------------------------------|-------------------------|
| id             | UUID        | PRIMARY KEY                                            |                         |
| user_id        | UUID        | NOT NULL, FK → users.id (cascade delete)              | Владелец задачи         |
| task_id        | UUID        | NOT NULL, FK → tasks.id (cascade delete), UNIQUE       | Задача парсинга         |
| bytes_total    | BIGINT      | NOT NULL, DEFAULT 0, CHECK ≥ 0                         | Объём трафика, байт     |
| requests_total | INTEGER     | NOT NULL, DEFAULT 0, CHECK ≥ 0                         | Число ответов           |
| captcha_total  | INTEGER     | NOT NULL, DEFAULT 0, CHECK ≥ 0 и ≤ requests_total      | Число ответов с капчей  |
| created_at     | TIMESTAMPTZ | DEFAULT now()                                          |                         |

**Индексы:** UNIQUE (task_id), INDEX (user_id).

> UNIQUE (task_id) делает запись идемпотентной: повтор задачи после снятия
> капчи обновляет ту же строку, а не создаёт дубль (иначе расход квоты и
> себестоимость удваивались бы).

---

## 4. Связи между таблицами
users 1 ─────── 1 user_profiles
│
│ 1
│
└─── < vacancies > 1 ─────── 0..1 analyses
│
│ 1
│
└─── 0..1 cover_letters
users 1 ─────── < tasks
users 1 ─────── < refresh_tokens
vacancies 1 ─── < tasks (опционально)

# Монетизация и учёт трафика (§3.8–§3.11). Все связи — 1:N от users,
# каскадные: удаление аккаунта (docs/03 §10) убирает их автоматически.
users 1 ─────── 0..1 subscriptions
users 1 ─────── < usage_counters
users 1 ─────── < proxy_usage_logs
tasks 1 ─────── 0..1 proxy_usage_logs
# payment_events связан с пользователем БЕЗ внешнего ключа: при удалении
# аккаунта запись обезличивается (user_id → NULL), но сохраняется.
users 1 ─────── < payment_events (без FK, обезличивается)
text---

## 5. Статусы вакансий (vacancies.status)

| Значение       | Описание                                      |
|----------------|-----------------------------------------------|
| `raw`          | Только добавлена, анализа нет                 |
| `analyzed`     | Есть анализ, письма нет                       |
| `letter_ready` | Есть анализ + сопроводительное письмо         |
| `applied`      | Пользователь отметил, что откликнулся         |
| `error`        | Ошибка при парсинге или анализе               |

---

## 6. Рекомендации по индексам и производительности

- Обязательный уникальный индекс: `(user_id, hh_vacancy_id)`
- Частые фильтры: `(user_id, status)`, `(user_id, created_at DESC)`
- Для очереди задач: частичный индекс по `status = 'pending'`
- JSONB-поля (`match_details`, `payload`, `result`) — при необходимости добавлять GIN-индексы