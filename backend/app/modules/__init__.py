"""Модули Modular-Flow (docs/01_ARCHITECTURE.md §3).

Каждый модуль — независимый пакет:
    auth              — регистрация, авторизация, JWT
    user_profile      — резюме, настройки, порог матчинга, compact_resume
    parsing           — три режима сбора вакансий (авто/группа/вручную)
    anti_ban              — прокси, fingerprints, антибан hh.ru
    vacancy_storage   — хранение, дедупликация, статусы вакансий
    queue_manager     — единая очередь задач, контроль параллелизма
    analysis_letter   — анализ, матчинг, генерация сопроводительных писем
    realtime          — WebSocket-доставка событий на frontend
"""
