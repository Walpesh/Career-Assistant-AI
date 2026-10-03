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
| POST | `/auth/refresh` | Обновление access-токена | Refresh-token (cookie/тело) |
| POST | `/auth/logout` | Отзыв refresh-токена и очистка cookie | Refresh-cookie |
| POST | `/auth/ws-ticket` | Одноразовый тикет для WebSocket | Да |
| GET  | `/auth/me` | Текущий пользователь | Да |

### Безопасность токенов (security hardening)

- Access-токен передаётся в теле ответа и хранится клиентом **только в памяти**
  (не в localStorage).
- Refresh-токен отдаётся как **HttpOnly; Secure; SameSite=Strict** cookie
  (`REFRESH_COOKIE_NAME`, путь `/api/v1/auth`) и регистрируется в таблице
  `refresh_tokens` (хранится только SHA-256 хэш).
- `/auth/refresh` выполняет **ротацию**: старый токен отзывается, выдаётся новый.
  Повторное предъявление уже ротированного токена трактуется как кража (reuse):
  все активные сессии пользователя отзываются, ответ — `401 REFRESH_TOKEN_REUSE`.
- Ошибки при превышении лимитов: `429 { "detail": "Rate limit exceeded",
  "error_code": "RATE_LIMITED" }` + заголовок `Retry-After`.

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
| POST | `/profile/convert-resume` | Запустить сокращение резюме через LLM (асинхронно, → задача очереди) | Да |
| POST | `/profile/compress-resume` | Синхронный алиас `convert-resume` («Compress Resume») | Да |

**POST /profile/convert-resume** — ставит задачу `convert_resume` в LLM-очередь
Queue Manager (docs/04 §6) и отвечает **202**:

```json
{
  "task_id": "uuid-задачи",
  "status": "pending"
}
```

`compact_resume` обновляется воркером; готовность приходит WS-событием
`task.completed`, прогресс — `task.progress`. Пустой `resume_text` → 400
`RESUME_EMPTY`; Redis недоступен → 503 `QUEUE_UNAVAILABLE`.

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
  "preferred_work_formats": ["remote", "hybrid"],
  "analysis_preferences": "не хочу трудоустройство по ТК РФ, нужен удалённый формат",
  "resume_addition": "Готов к собеседованию в удобное время."
}
```

- `analysis_preferences` — свободный текст (до 2000 символов). Пишется обычным
  языком, передаётся в промпт этапа анализа (docs/05 §4): нейросеть сама решает,
  противоречит ли вакансия пожеланиям, понижает `match_score` и описывает
  противоречия в `weaknesses`.
- `resume_addition` — свободный текст (до 2000 символов). Если поле непустое, в
  конце обработки вакансии скриптовым методом дописывается в конец письма
  «с красной строки», без участия LLM (docs/05 §5, §6).
- Оба поля очищаются пустой строкой; отсутствие поля в теле запроса ничего не меняет.

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
  "max_pages": 5,
  "blacklist_enabled": true,
  "blacklist_words": ["ТК РФ", "ГПХ"]
}
```

**POST /parsing/group**
```json
{
  "search_url": "https://novokuznetsk.hh.ru/vacancies/razrabotchik",
  "max_pages": 3,
  "blacklist_enabled": true,
  "blacklist_words": ["ТК РФ"]
}
```

- `blacklist_enabled` (bool, по умолчанию `false`) — тумблер чёрного списка слов.
- `blacklist_words` (массив строк, максимум 50) — слова, при совпадении с
  содержимым вакансии она не сохраняется в БД; ранее сохранённая удаляется
  (docs/04 §4.9). Поля поддерживаются в `auto` и `group`.
- При `blacklist_enabled: false` слова игнорируются и в `tasks.payload` не
  сохраняются — парсинг идёт строго по старым фильтрам.

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
| GET | `/tasks/{task_id}` | Статус конкретной задачи (включая `progress_stage` / `progress_message`) | Да |
| POST | `/tasks/{task_id}/cancel` | Отменить задачу (если возможно) | Да |
| POST | `/tasks/{task_id}/resume` | Возобновить задачу `waiting_captcha` после ручного обхода капчи | Да |

**POST /tasks/{task_id}/cancel**
- Доступно для `pending` / `processing` / `waiting_captcha`; `completed` → 400
  `TASK_COMPLETED`, `failed` → 400 `TASK_FAILED`.
- Job **явно снимается из очереди Redis**, статус → `failed`
  (`error_message = "Отменено пользователем"`, `finished_at = now()`).
- 409 `VACANCY_DELETED` — связанная вакансия удалена; 409 `VACANCY_APPLIED` —
  статус вакансии `applied` (терминальный, docs/02 §5).
- Ответ: `{ "task_id": "...", "status": "failed", "cancelled": true }`;
  дополнительно публикуются события `task.cancelled` и `task.failed`.

**POST /tasks/{task_id}/resume**
- Только для `waiting_captcha` (иначе 409 `TASK_NOT_WAITING_CAPTCHA`):
  статус → `pending`, `error_message` очищается, job ставится в Redis заново
  (при живом «старом» ключе — с уникальным суффиксом, чтобы дедупликация не
  потеряла задачу).
- Ответ: `{ "task_id": "...", "status": "pending", "resumed": true }`,
  событие `task.resumed`.

---

## 8. WebSocket

**Подключение (рекомендуемый способ):**

1. Клиент делает `POST /api/v1/auth/ws-ticket` с access-токеном → `{ "ticket": "<uuid>" }`.
2. Открывает `ws://<host>/api/v1/ws?ticket=<ticket>`.
3. Тикет одноразовый (TTL ~30 с, хранится в Redis) и сгорает после первого
   использования — access-токен в URL не попадает в логи и историю.

**Fallback:** `ws://<host>/api/v1/ws?token=<access_token>` по-прежнему
поддерживается (legacy/скрипты), но предпочтительнее тикет.

### Основные события (сервер → клиент)

| event | Описание | Пример payload |
|-------|----------|----------------|
| `task.created` | Создана новая задача | `{ "task_id": "...", "task_type": "parse_auto" }` |
| `task.progress` | Обновление прогресса | `{ "task_id": "...", "current": 12, "total": 47, "message": "Парсинг вакансии 12/47" }` |
| `task.completed` | Задача успешно завершена | `{ "task_id": "...", "result": {...} }` |
| `task.failed` | Задача завершилась ошибкой (или приостановлена: `status = "waiting_captcha"`) | `{ "task_id": "...", "error": "Captcha detected" }` |
| `task.cancelled` | Задача отменена пользователем | `{ "task_id": "...", "status": "failed", "error": "Отменено пользователем" }` |
| `task.resumed` | Задача возобновлена после капчи (`waiting_captcha` → `pending`) | `{ "task_id": "...", "status": "pending" }` |
| `vacancy.updated` | Изменилась вакансия (статус, данные) | `{ "vacancy_id": "...", "status": "letter_ready" }` |
| `analysis.ready` | Готов анализ | `{ "vacancy_id": "...", "match_score": 82 }` |
| `letter.ready` | Готово сопроводительное письмо | `{ "vacancy_id": "..." }` |
| `popup` | Сообщение для Fadeout-action-popup | `{ "type": "info|success|warning|error", "title": "...", "message": "..." }` |

### Клиент → Сервер
На первом этапе клиент только слушает. Команды отправляются через REST.
Для поддержания соединения клиент отправляет `{"event": "ping"}` — сервер отвечает
`{"event": "pong"}`. Все сообщения сервера — валидный JSON вида `{ "event": ..., "data": {...} }`.

### Коды закрытия канала
| Код  | Значение                                                                 |
|------|--------------------------------------------------------------------------|
| 1000 | Штатное закрытие                                                        |
| 4001 | Соединение вытеснено более новым подключением того же пользователя        |
| 4401 | Токен невалиден или истёк (клиент обновляет токен и переподключается)      |

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
