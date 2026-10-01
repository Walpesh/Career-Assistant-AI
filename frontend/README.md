# Frontend — Career-Assistant-AI

SPA на **HTML5 + Tailwind CSS + Vanilla JS (ES-модули)**. Без сборки: Tailwind подключается
через Play CDN, компонентные классы описаны в `<style type="text/tailwindcss">` внутри `index.html`,
кастомные анимации и стили `Fadeout-action-popup` — в `css/styles.css`.

## Структура

```
frontend/
├── index.html                 # Shell приложения: экран авторизации + каркас SPA
├── assets/favicon.svg
├── css/styles.css             # Анимации popup, скроллбары, прогресс-штриховка
├── partials/                  # HTML-шаблоны вкладок и оверлеев (подгружаются по требованию)
│   ├── profile.html           # «Моё резюме»: резюме, compact_resume, навыки, порог матчинга
│   ├── dashboard.html         # Парсинг: Автопоиск / Групповой парсер / Ручное добавление + задачи
│   ├── analysis.html          # «Анализ и Отклик»: фильтры, список, batch-панель, письма
│   └── modals.html            # Модалка анализа, drawer письма, confirm-диалог
└── js/
    ├── main.js                # Bootstrap: сессия, WS, lazy-mount вкладок
    ├── config.js              # API_BASE / WS URL / DEMO (?demo=1, ?api=...)
    ├── core/
    │   ├── api.js             # REST-клиент по docs/03_API_CONTRACTS.md
    │   ├── ws.js              # WebSocket-клиент (реконнект, события ws:*)
    │   ├── bus.js             # Внутренняя шина событий
    │   ├── session.js         # JWT-токены в localStorage
    │   ├── state.js           # Общее состояние (профиль, задачи, фильтры)
    │   ├── tasks.js           # Трекинг задач (GET /tasks + WS task.*)
    │   ├── utils.js           # Форматирование, clipboard, download, debounce
    │   └── mock.js            # Демо-данные для ?demo=1 (строго по контрактам)
    ├── components/
    │   ├── fadeout-action-popup.js  # Кастомный элемент <fadeout-action-popup>
    │   ├── tag-input.js             # Ввод тегов (ключевые слова, навыки)
    │   ├── progress.js              # Прогресс-бары задач
    │   ├── badges.js                # Бейджи статусов/источников, match-score теги
    │   └── overlay.js               # Модалки/drawer/confirm + Escape/backdrop
    └── views/
        ├── auth.js            # Вход / Регистрация
        ├── shell.js           # Навигация, топбар, WS-индикатор, logout
        ├── profile.js         # Вкладка «Моё резюме»
        ├── dashboard.js       # Вкладка «Парсинг-дашборд» (3 режима + задачи)
        ├── analysis.js        # Вкладка «Анализ и Отклик»
        └── log-panel.js       # Реал-тайм журнал + мини-прогрессы активных задач
```

## Запуск

```powershell
# из корня репозитория
python -m http.server 5500 --directory frontend
```

- `http://localhost:5500/` — обычный режим (API берётся с `http://localhost:8000/api/v1`).
- `http://localhost:5500/?demo=1` — демо-режим с фиктивными данными (для проверки вёрстки).
- `http://localhost:5500/?api=<url>` — переопределить адрес API (сохраняется в localStorage).

## Соглашения

- **Никакой бизнес-логики в HTML** — разметка в `partials/`, вся интерактивность в `views/*`.
- Обработка кликов — делегирование по `[data-action]` (см. соответствующий `views/*`).
- WS-события транслируются в шину как `ws:<event>` (например `ws:task.progress`),
  сырой поток — `ws:event` (используется журналом).
- Уведомления: `popup.success(title, message)` и т.п. (компонент `Fadeout-action-popup`),
  событие `popup` из WebSocket автоматически показывается как toast.
- `compact_resume` и порог матчинга подтягиваются из `GET /profile` (см. docs/05_LLM_PIPELINE.md).
