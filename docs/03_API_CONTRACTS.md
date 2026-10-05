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
| POST | `/auth/register` | Регистрация (JWT **не** выдаётся, отправляется OTP-код) | Нет |
| POST | `/auth/verify-email` | Подтверждение email кодом → пара JWT | Нет |
| POST | `/auth/resend-code` | Повторная отправка OTP-кода (1 раз / 60 сек на email) | Нет |
| POST | `/auth/login` | Вход (получение JWT) | Нет |
| POST | `/auth/refresh` | Обновление access-токена | Refresh-token (cookie/тело) |
| POST | `/auth/logout` | Отзыв refresh-токена и очистка cookie | Refresh-cookie |
| POST | `/auth/ws-ticket` | Одноразовый тикет для WebSocket | Да |
| GET  | `/auth/me` | Текущий пользователь | Да |

### Подтверждение email по OTP-коду

Регистрация двухшаговая: сначала создаётся аккаунт, затем подтверждается
владение email. **Пара JWT выдаётся только после подтверждения.**

| Шаг | Запрос | Ответ |
|-----|--------|-------|
| 1 | `POST /auth/register` | `201 { "message": "Verification code sent to email", "email": … }` |
| 2 | `POST /auth/verify-email` | `200 { "access_token", "refresh_token", "token_type" }` |

Правила:

- Код — 6 цифр, генерируется криптостойко (`secrets`), в БД хранится **только
  HMAC-SHA256-хэш** (`email_otps.otp_code_hash`); сам код живёт в письме.
- Срок действия — 10 минут (`OTP_TTL_MINUTES`), максимум 5 неверных попыток
  (`OTP_MAX_ATTEMPTS`), после чего код блокируется до повторной отправки.
- Успешный код удаляется и не может быть использован повторно; повторная
  отправка заменяет предыдущий код (действует только последний).
- `POST /auth/login` для неподтверждённого аккаунта — `403 EMAIL_NOT_VERIFIED`.
- `POST /auth/resend-code` — не чаще 1 раза в 60 сек на email
  (`OTP_RESEND_INTERVAL_SECONDS`), иначе `429 RATE_LIMITED` + `Retry-After`.
  Для несуществующего и уже подтверждённого email ответ такой же
  (200 без отправки) — иначе эндпоинт раскрывал бы существующие адреса.
- `POST /auth/register` для **неподтверждённого** email не конфликтует, а
  обновляет пароль и перевыпускает OTP-код (владелец мог не получить письмо):
  `201 { "message": "Verification code sent to email", "email": … }`. Старый код
  при этом перестаёт действовать — владение email подтверждается заново.
  `409 EMAIL_TAKEN` отдаётся только для уже **подтверждённого** email, где
  смена пароля означала бы захват чужого аккаунта.

### Доставка OTP-кода и отказ почтового сервиса

Код существует только в письме, поэтому «успешный» ответ при недоставленном
письме — это ложное обещание: пользователь ждёт код, которого не будет.

| Окружение | SMTP не настроен | Отправка не удалась |
|-----------|------------------|---------------------|
| development | `201`/`200`, OTP-код пишется в лог приложения (локальная разработка не блокируется) | `201`/`200`, причина — в логе |
| production | запрещено на старте (fail-fast в `Settings`) | `503 { "detail": "Email service unavailable", "error_code": "SMTP_UNAVAILABLE" }` |

Оба ответа покрывают `POST /auth/register` и `POST /auth/resend-code`;
в production отправка выполняется синхронно, иначе её результат нельзя
учесть в ответе. Проверка доступности SMTP есть в `GET /health/ready`
(ключ `smtp`: `up` | `down` | `skipped`, `skipped` — если `SMTP_HOST` пуст;
`skipped` не считается деградацией).

Ошибки `POST /auth/verify-email` — `400` с `error_code`:
`OTP_INVALID` (неверный код, попытка засчитана), `OTP_NOT_FOUND` (код не
запрашивался или уже использован), `OTP_EXPIRED` (10 минут истекли),
`OTP_LOCKED` (5 неверных попыток). `error_code` обязателен: клиент показывает
пользователю текст сервера и понимает, когда можно запросить новый код.

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

Ответ `201` (JWT нет — аккаунт не подтверждён):
```json
{
  "message": "Verification code sent to email",
  "email": "user@example.com"
}
```

**POST /auth/verify-email**
```json
{
  "email": "user@example.com",
  "code": "123456"
}
```

**POST /auth/resend-code**
```json
{
  "email": "user@example.com"
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
| POST | `/vacancies/manual` | Ручное добавление по прямой ссылке hh.ru | Да |

### POST /vacancies/manual

Тело: `{ "vacancy_url": "https://hh.ru/vacancy/<id>" }`. Эндпоинт ставит
задачу `parse_manual` в очередь парсинга (docs/04 §4.3) и возвращает
`202 { task_id, status: "pending" }`.

Ошибки: `400 INVALID_VACANCY_URL` (ссылка не на hh.ru/vacancy/&lt;id&gt;),
`409 EMAIL_TAKEN`-подобные конфликты не применяются; `503 QUEUE_UNAVAILABLE`,
если Redis недоступен (задача переводится в `failed`).

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
| 413 | Тело запроса слишком велико (вебхук) |
| 429 | Слишком много запросов (`RATE_LIMITED`) либо суточная квота тарифа исчерпана (`QUOTA_EXCEEDED`) |
| 500 | Внутренняя ошибка сервера |
| 503 | Зависимость недоступна: очередь (`QUEUE_UNAVAILABLE`), тикеты WS (`TICKET_UNAVAILABLE`), почтовый сервис (`SMTP_UNAVAILABLE`) |

---

## 10. Account & Privacy Endpoints

Право на доступ и право на удаление по 152-ФЗ ст. 14/21 и GDPR ст. 15/17.
Все эндпоинты требуют Bearer JWT: анонимно получить или удалить данные нельзя.

| Метод | URL | Описание | Auth |
|-------|-----|----------|------|
| GET | `/account/export` | Полная выгрузка персональных данных (JSON-файл) | Да |
| GET | `/account/summary` | Сводка объёма данных (перед удалением) | Да |
| DELETE | `/account` | Каскадное удаление аккаунта и всех данных | Да |

### GET /account/export

Возвращает единый JSON-пакет всех персональных данных пользователя:

```json
{
  "format_version": "1.0",
  "generated_at": "2026-10-04T10:00:00+00:00",
  "user": { "id": "uuid", "email": "user@example.com", "is_active": true, "created_at": "..." },
  "profile": { "full_name": "...", "resume_text": "...", "compact_resume": "...", "skills": ["Python"] },
  "subscription": { "tier": "pro", "status": "active" },
  "vacancies": [ { "id": "uuid", "hh_vacancy_id": "123", "title": "...", "status": "analyzed" } ],
  "analyses": [ { "vacancy_id": "uuid", "match_score": 82, "summary": "..." } ],
  "cover_letters": [ { "vacancy_id": "uuid", "content": "...", "version": 1 } ],
  "tasks": [ { "id": "uuid", "task_type": "parse_auto", "status": "completed" } ],
  "usage_counters": [ { "day": "2026-10-04", "quota_kind": "parse", "used": 3 } ],
  "proxy_usage_logs": [ { "task_id": "uuid", "bytes_total": 4096, "captcha_total": 1 } ],
  "excluded_by_design": [
    "password_hash (секрет аутентификации, не персональные данные)",
    "refresh_tokens.hashed_token (материал сессии; удаляется с аккаунтом)"
  ]
}
```

Заголовки ответа: `Cache-Control: no-store` (персональные данные не кэшируются),
`Content-Disposition: attachment`. Пагинации нет намеренно: цель выгрузки —
полнота, а не скорость ответа.

`password_hash` и хэши refresh-токенов **не выгружаются**: это технические
секреты, а не персональные данные. Факт исключения отражён в `excluded_by_design`.

### DELETE /account

Каскадно удаляет профиль, вакансии, анализы, сопроводительные письма, историю
задач, подписку, счётчики квот, логи прокси-трафика и refresh-токены.
Возвращает отчёт по фактическому числу удалённых строк — это подтверждение
исполнения запроса (152-ФЗ ст. 21):

```json
{
  "deleted": true,
  "report": {
    "users": 1,
    "user_profiles": 1,
    "vacancies": 12,
    "analyses": 10,
    "cover_letters": 8,
    "tasks": 30,
    "proxy_usage_logs": 6,
    "usage_counters": 4,
    "subscriptions": 1,
    "refresh_tokens": 2,
    "payment_events_anonymized": 1,
    "total_rows_deleted": 75
  },
  "message": "Аккаунт и все персональные данные удалены..."
}
```

Отчёт **не содержит** идентификатора пользователя (он сам по себе ПД).
Записи `payment_events` обезличиваются (`user_id → NULL`), а не удаляются:
иначе повторная доставка вебхука после удаления аккаунта воскресила бы подписку.
Refresh-cookie удаляется вместе с аккаунтом.

---

## 11. Billing Endpoints (тарифы, квоты, платёжные вебхуки)

| Метод | URL | Описание | Auth |
|-------|-----|----------|------|
| GET | `/billing/tiers` | Каталог тарифов и суточных квот | Нет |
| GET | `/billing/usage` | Текущий тариф и расход квот за сутки | Да |
| GET | `/billing/subscription` | Состояние подписки | Да |
| POST | `/billing/webhook/{provider}` | Вебхук платёжного шлюза | Подпись HMAC |

### Тарифы и суточные квоты

Квоты считаются по календарным суткам **UTC**. `-1` означает безлимит.

| Тариф | Парсинг/сутки | Письма/сутки | Анализы/сутки | Трафик/сутки |
|-------|---------------|--------------|---------------|--------------|
| `free` | 5 | 10 | 30 | 200 МБ |
| `pro` | 50 | 200 | 1000 | 5000 МБ |
| `enterprise` | без лимита | без лимита | без лимита | без лимита |

Индивидуальные переопределения задаются в `subscriptions.daily_*`
(enterprise-контракты). Отсутствие строки `subscriptions` равносильно `free`,
а подписка со статусом не `active` (отменённая/просроченная) прав не даёт.

### Проверка квот

Квота списывается **до** постановки задачи в очередь. Порядок принципиален:
при обратном порядке пользователь получил бы «висящую» задачу, которую
воркер тут же отклонил бы по лимиту.

- `POST /parsing/auto|group|manual` → квота `parse` (1 за запуск);
- `POST /analysis/run` → квота `analysis` × число вакансий, и для режимов
  `letter` / `analyze_and_letter` / `auto` также квота `letter` × число вакансий;
- `POST /profile/convert-resume` → квота `analysis` (1).

При исчерпании — `429` в формате §1:

```json
{
  "detail": "Исчерпана суточная квота «parse» для тарифа free: 5/5. Квота обновится 2026-10-05T00:00:00+00:00...",
  "error_code": "QUOTA_EXCEEDED"
}
```

Начисление атомарно (`INSERT … ON CONFLICT DO UPDATE … WHERE`), поэтому
параллельные запросы физически не могут превысить лимит.

### POST /billing/webhook/{provider}

`provider`: `yookassa` | `cloudpayments` | `stripe`. Авторизация Bearer не
используется — вместо неё проверка подписи HMAC:

| Провайдер | Заголовок подписи | Алгоритм |
|-----------|-------------------|----------|
| `yookassa` | `X-Signature: sha256=<hex>` | HMAC-SHA256 от тела запроса |
| `cloudpayments` | `Content-HMAC: sha1=<hex>` (или `sha256=`) | HMAC-SHA1/SHA256 от тела |
| `stripe` | `Stripe-Signature: t=…,v1=…` | HMAC-SHA256 от `"<t>.<body>"` + проверка окна времени |

Подпись считается по **сырым байтам** тела, а не по пересобранному JSON:
у шлюзов каноническая сериализация. Сравнение — за постоянное время
(`hmac.compare_digest`), иначе подпись можно было бы перебирать по времени
ответа.

**Идемпотентность:** ключ `(provider, external_event_id)` в `payment_events`.
Повторная доставка возвращает `{"status": "duplicate"}` и не меняет тариф.
Резервирование ключа атомарно, поэтому два одновременных повтора не пройдут оба.

```json
{ "status": "processed", "event_type": "payment.succeeded", "provider": "yookassa", "tier": "pro" }
```

Ошибки: `400 WEBHOOK_SIGNATURE_INVALID` (подпись не сошлась или
`BILLING_WEBHOOK_SECRET` не настроен — принимать событие в этом случае означало
бы позволить любому выдать себя за платёжный шлюз),
`400 WEBHOOK_PROVIDER_UNSUPPORTED`, `413 WEBHOOK_BODY_TOO_LARGE`.

Отмена подписки (`subscription.canceled`) не отбирает оплаченный период:
доступ сохраняется до конца оплаченного срока, затем тариф возвращается в `free`.
```
