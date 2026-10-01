```markdown
# 03_API_CONTRACTS.md
# API-контракты Career-Assistant-AI

## 1. Общие соглашения

- Базовый префикс: `/api/v1`
- Авторизация: Bearer JWT (заголовок `Authorization: Bearer <token>`)
- Формат ответов: JSON
- Все ошибки возвращаются в едином формате:

```json
{
  "detail": "Человекочитаемое сообщение",
  "error_code": "OPTIONAL_CODE"
}
```

- Пагинация (где применимо): `?page=1&size=20`
- Реал-тайм события доставляются только через WebSocket

---

## 2. Auth Endpoints

| Метод | URL | Описание | Auth |
|-------|-----|----------|------|
| POST | `/auth/register` | Регистрация | Нет |
| POST | `/auth/login` | Вход (получение JWT) | Нет |
| POST | `/auth/refresh` | Обновление access-токена | Refresh-token |
| GET  | `/auth/me` | Текущий пользователь | Да |

### Примеры тел запросов

**POST /auth/register**
```json
{
  "email": "user@example.com",
  "password": "strongpassword"
}
```

**POST /auth/login**
```json
{
  "email": "user@example.com",
  "password": "strongpassword"
}
```

---

## 3. Profile Endpoints

| Метод | URL | Описание | Auth |
|-------|-----|----------|------|
| GET | `/profile` | Получить профиль | Да |
| PUT | `/profile` | Обновить профиль | Да |
| POST | `/profile/convert-resume` | Запустить сокращение резюме через LLM | Да |
| POST | `/profile/compress-resume` | Алиас `convert-resume` («Compress Resume») | Да |

**PUT /profile** (частичное обновление поддерживается)
```json
{
  "full_name": "Иван Иванов",
  "resume_text": "Полный текст резюме...",
  "skills": ["Python", "FastAPI", "PostgreSQL"],
  "experience_years": 4.5,
  "desired_salary_from": 180000,
  "desired_salary_to": 250000,
  "match_threshold": 75,
  "preferred_work_formats": ["remote", "hybrid"]
}
```

---

## 4. Vacancies Endpoints

| Метод | URL | Описание | Auth |
|-------|-----|----------|------|
| GET | `/vacancies` | Список вакансий пользователя (с фильтрами) | Да |
| GET | `/vacancies/{vacancy_id}` | Детальная информация о вакансии | Да |
| DELETE | `/vacancies/{vacancy_id}` | Удалить вакансию | Да |
| PATCH | `/vacancies/{vacancy_id}/status` | Изменить статус (например, applied) | Да |

### Фильтры для GET /vacancies
- `status` — raw / analyzed / letter_ready / applied / error
- `source` — auto / group / manual
- `search` — поиск по названию и компании
- `min_match_score`
- `page`, `size`

---

## 5. Parsing Endpoints (запуск сбора)

| Метод | URL | Описание | Auth |
|-------|-----|----------|------|
| POST | `/parsing/auto` | Запуск Автопоиска | Да |
| POST | `/parsing/group` | Запуск Группового парсера | Да |
| POST | `/parsing/manual` | Добавление одной вакансии по ссылке | Да |

### Примеры тел запросов

**POST /parsing/auto**
```json
{
  "keywords": ["python", "fastapi", "backend"],
  "employment_forms": ["full", "gph"],
  "work_formats": ["remote", "hybrid"],
  "schedules": ["fullDay", "flexible"],
  "match_threshold": 80,
  "max_pages": 5
}
```

**POST /parsing/group**
```json
{
  "search_url": "https://novokuznetsk.hh.ru/vacancies/razrabotchik",
  "max_pages": 3
}
```

**POST /parsing/manual**
```json
{
  "vacancy_url": "https://novokuznetsk.hh.ru/vacancy/137866214",
  "run_analysis": false
}
```

Все три эндпоинта возвращают:
```json
{
  "task_id": "uuid-задачи",
  "status": "pending"
}
```

---

## 6. Analysis & Letter Endpoints

| Метод | URL | Описание | Auth |
|-------|-----|----------|------|
| POST | `/analysis/run` | Запуск обработки вакансий | Да |
| GET  | `/analysis/{vacancy_id}` | Получить анализ вакансии | Да |
| GET  | `/letters/{vacancy_id}` | Получить сопроводительное письмо | Да |

**POST /analysis/run**
```json
{
  "vacancy_ids": ["uuid1", "uuid2"],
  "mode": "auto",               // analyze | letter | analyze_and_letter | auto
  "match_threshold": 75         // опционально, переопределяет профиль
}
```

Возвращает `task_id`.

---

## 7. Tasks Endpoints

| Метод | URL | Описание | Auth |
|-------|-----|----------|------|
| GET | `/tasks` | Список задач пользователя | Да |
| GET | `/tasks/{task_id}` | Статус конкретной задачи | Да |
| POST | `/tasks/{task_id}/cancel` | Отменить задачу (если возможно) | Да |

---

## 8. WebSocket

**Подключение:** `ws://<host>/api/v1/ws?token=<access_token>`

### Основные события (сервер → клиент)

| event | Описание | Пример payload |
|-------|----------|----------------|
| `task.created` | Создана новая задача | `{ "task_id": "...", "task_type": "parse_auto" }` |
| `task.progress` | Обновление прогресса | `{ "task_id": "...", "current": 12, "total": 47, "message": "Парсинг вакансии 12/47" }` |
| `task.completed` | Задача успешно завершена | `{ "task_id": "...", "result": {...} }` |
| `task.failed` | Задача завершилась ошибкой | `{ "task_id": "...", "error": "Captcha detected" }` |
| `vacancy.updated` | Изменилась вакансия (статус, данные) | `{ "vacancy_id": "...", "status": "letter_ready" }` |
| `analysis.ready` | Готов анализ | `{ "vacancy_id": "...", "match_score": 82 }` |
| `letter.ready` | Готово сопроводительное письмо | `{ "vacancy_id": "..." }` |
| `popup` | Сообщение для Fadeout-action-popup | `{ "type": "info|success|warning|error", "title": "...", "message": "..." }` |

### Клиент → Сервер
На первом этапе клиент только слушает. Команды отправляются через REST.

---

## 9. Коды ответов

| Код | Значение |
|-----|----------|
| 200 | Успех |
| 201 | Создано |
| 202 | Принято в обработку (возвращается task_id) |
| 400 | Ошибка валидации |
| 401 | Не авторизован |
| 403 | Нет доступа |
| 404 | Не найдено |
| 409 | Конфликт (например, дубликат) |
| 429 | Слишком много запросов |
| 500 | Внутренняя ошибка сервера |
```
