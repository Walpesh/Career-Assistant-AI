# Frontend — Career-Assistant-AI

SPA на **HTML5 + Tailwind CSS (CLI-сборка) + Vanilla JS (ES-модули)**.

Tailwind собирается на этапе сборки в статический `css/styles.min.css`
(Play CDN в production запрещён — он исполняет JS на клиенте и несовместим
со строгой CSP, см. `docs/01_ARCHITECTURE.md` §8). Тема вынесена
в `tailwind.config.js`, компонентные классы — в `src/input.css`.

## Сборка

```bash
cd frontend
npm ci
npm run build      # production: css/styles.min.css + assets/manifest.json (SRI)
npm run watch:css  # разработка: пересборка при изменениях
npm run verify     # проверка для CI (хэши, внешние ресурсы, синтаксис JS)
```

`css/styles.min.css` коммитится в репозиторий — сборка Docker-образа
не требует Node.js. После любого изменения `src/` или `tailwind.config.js`
нужно выполнить `npm run build` и закоммитить результат: иначе
`npm run verify` (и CI) упадёт.

## Структура

```
frontend/
├── index.html                 # Shell приложения: экран авторизации + каркас SPA
├── 404.html                   # Кастомная страница ошибки (nginx error_page)
├── assets/
│   ├── favicon.svg
│   └── manifest.json          # SRI-хэши (sha384) статических ресурсов
├── fonts/                     # Self-hosted Inter (152-ФЗ / GDPR), имена с content-hash
│   ├── inter-latin.3100e775.woff2
│   ├── inter-latin-ext.34b9c504.woff2
│   ├── inter-cyrillic.71d5ee93.woff2
│   └── inter-cyrillic-ext.ca157063.woff2
├── src/                       # ИСТОЧНИКИ стилей (в git ради правок)
│   ├── input.css              # @tailwind + компонентные классы (@apply)
│   ├── fonts.css              # @font-face для self-hosted Inter
│   └── custom.css             # Кастомные компоненты вне Tailwind
├── css/
│   └── styles.min.css         # СБОРКА (коммитится, отдаётся в production)
├── tools/
│   └── build-assets.mjs       # Проверка/обновление SRI-хэшей и content-hash
├── partials/                  # HTML-шаблоны вкладок и оверлеев (по требованию)
│   ├── profile.html           # «Моё резюме»: резюме, compact_resume, навыки, порог
│   ├── dashboard.html         # Парсинг: Автопоиск / Групповой / Ручное + задачи
│   ├── analysis.html          # «Анализ и Отклик»: фильтры, список, batch, письма
│   └── modals.html            # Модалка анализа, drawer письма, confirm-диалог
└── js/
    ├── main.js                # Bootstrap: сессия, WS, lazy-mount вкладок
    ├── config.js              # API_BASE / WS URL / DEMO (?demo=1, ?api=...)
    ├── core/
    │   ├── api.js             # REST-клиент по docs/03_API_CONTRACTS.md
    │   ├── ws.js              # WebSocket-клиент (реконнект, watchdog, mobile Safari)
    │   ├── bus.js             # Внутренняя шина событий
    │   ├── session.js         # JWT: access — в памяти, refresh — HttpOnly cookie
    │   ├── state.js           # Общее состояние (профиль, задачи, фильтры)
    │   ├── tasks.js           # Трекинг задач (GET /tasks + WS task.*)
    │   ├── utils.js           # Форматирование, clipboard, download, debounce
    │   ├── degradation.js     # Баннер деградации (LLM/очередь/WS недоступны)
    │   ├── partials.js        # Загрузка HTML-шаблонов вкладок
    │   └── mock.js            # Демо-данные для ?demo=1 (строго по контрактам)
    ├── components/
    │   ├── fadeout-action-popup.js  # Кастомный <fadeout-action-popup>
    │   ├── tag-input.js             # Ввод тегов (ключевые слова, навыки)
    │   ├── progress.js              # Прогресс-бары (role=progressbar + ARIA)
    │   ├── badges.js                # Бейджи статусов/источников, match-score
    │   └── overlay.js               # Модалки/drawer/confirm + Escape/backdrop
    └── views/
        ├── auth.js            # Вход / Регистрация
        ├── shell.js           # Навигация, топбар, WS-индикатор, logout
        ├── profile.js         # Вкладка «Моё резюме»
        ├── dashboard.js       # Вкладка «Парсинг-дашборд» (3 режима + задачи)
        ├── analysis.js        # Вкладка «Анализ и Отклик»
        └── log-panel.js       # Реал-тайм журнал + мини-прогрессы задач
```


## Запуск

```powershell
# из корня репозитория
python -m http.server 5500 --directory frontend
```

- `http://localhost:5500/` — обычный режим (API берётся с `http://localhost:8000/api/v1`).
- `http://localhost:5500/?demo=1` — демо-режим с фиктивными данными (для проверки вёрстки).
- `http://localhost:5500/?api=<url>` — переопределить адрес API (сохраняется в localStorage).

> При статическом сервере плейсхолдер `nonce="__CSP_NONCE__"` остаётся
> в разметке — это безвредно, CSP-заголовки там не отправляются.
> Подстановка nonce работает при отдаче через nginx или FastAPI.

## Безопасность и кэширование

- **CSP без `'unsafe-inline'`** в `script-src`: единственный инлайн-скрипт
  (bootstrap `window.APP_CONFIG`) получает nonce от сервера.
  Инлайновые `style`-атрибуты не используются — ширина прогресс-бара
  выставляется через CSSOM, иначе строгий `style-src 'self'` их заблокирует.
- **SRI** (`integrity` + `crossorigin`) на `styles.min.css` и preload-ссылках
  шрифтов; хэши обновляет `tools/build-assets.mjs`.
- **Кэш**: `index.html` / `404.html` — `no-cache`; `/fonts/<name>.<hash>.woff2` —
  `public, max-age=31536000, immutable`. Подробно — `docs/01_ARCHITECTURE.md` §8.4.

## Соглашения

- **Никакой бизнес-логики в HTML** — разметка в `partials/`, вся интерактивность в `views/*`.
- Обработка кликов — делегирование по `[data-action]` (см. соответствующий `views/*`).
- WS-события транслируются в шину как `ws:<event>` (например `ws:task.progress`),
  сырой поток — `ws:event` (используется журналом).
- Уведомления: `popup.success(title, message)` и т.п. (компонент `Fadeout-action-popup`),
  событие `popup` из WebSocket автоматически показывается как toast.
- `compact_resume` и порог матчинга подтягиваются из `GET /profile` (см. docs/05_LLM_PIPELINE.md).
- **Tailwind-классы, собираемые в JS**, должны встречаться в коде литералом:
  они сканируются по `js/**/*.js`. Динамическая сборка имени (`'bg-' + color`)
  в сборку не попадёт — используйте полные строки или `safelist`.
