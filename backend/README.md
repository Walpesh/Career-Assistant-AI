# Backend — Career-Assistant-AI (FastAPI, Modular-Flow)

Скелет приложения по [docs/01_ARCHITECTURE.md](../docs/01_ARCHITECTURE.md).
На текущем этапе создана структура и точки подключения роутеров каждого модуля;
бизнес-логика появится в следующих итерациях. Эндпоинты — по контракту
[docs/03_API_CONTRACTS.md](../docs/03_API_CONTRACTS.md).

## Структура

```
backend/
├── app/
│   ├── main.py                 # FastAPI-приложение: CORS, /health, монтирование frontend/
│   ├── core/
│   │   └── config.py           # Настройки (pydantic-settings, .env)
│   ├── api/v1/router.py        # Сборка роутеров всех модулей под /api/v1
│   └── modules/                # 8 модулей Modular-Flow
│       ├── auth/               # Регистрация, логин, JWT            → /auth
│       ├── user_profile/       # Профиль, резюме, порог матчинга    → /profile
│       ├── parsing/            # 3 режима сбора вакансий            → /parsing
│       ├── proxy_antiban/      # Прокси, fingerprints, антибан      (внутренний)
│       ├── vacancy_storage/    # Хранение, дедупликация, статусы    → /vacancies
│       ├── queue_manager/      # Единая очередь задач и прогресс    → /tasks
│       ├── analysis_letter/    # Анализ, матчинг, письма            → /analysis, /letters
│       └── realtime/           # WebSocket-доставка событий         → /ws
├── requirements.txt
└── .env.example
```

## Запуск

```powershell
cd backend
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env   # при необходимости отредактируйте
uvicorn app.main:app --reload --port 8000
```

- `http://localhost:8000/health` — проверка живости.
- `http://localhost:8000/api/v1/...` — REST по контракту.
- `http://localhost:8000/` — отдача собранного frontend (`../frontend`, если каталог существует).

## Правила модулей (docs/01 §5)

- Модули не лезут в чужие таблицы — только через публичные интерфейсы модуля.
- Долгие операции (парсинг, LLM) — обязательно через Queue Manager.
- LLM-запросы строго последовательно (один воркер).
- Парсинг — максимум 2 воркера на пользователя.
- Изменения статусов/прогресса публикуются в Realtime Module.
