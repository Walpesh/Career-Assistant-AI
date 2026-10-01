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
| status            | VARCHAR(32)      | NOT NULL, DEFAULT 'pending'             | pending / processing / completed / failed     |
| progress_current  | INTEGER          | DEFAULT 0                                | Текущий прогресс                              |
| progress_total    | INTEGER          | DEFAULT 0                                | Общее количество шагов                        |
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
vacancies 1 ─── < tasks (опционально)
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