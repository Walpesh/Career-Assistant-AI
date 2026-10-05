"""Настройки приложения (переменные окружения / .env)."""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

#: Значение JWT_SECRET по умолчанию, которое обязано быть изменено в production.
INSECURE_JWT_SECRET = "change-me-in-production"
#: Минимальная длина JWT-секрета в production (docs/03 §2 — HS256).
MIN_JWT_SECRET_LENGTH = 32

#: Каталог backend/ (backend/app/core/config.py → parents[2]).
#:
#: ``.env`` ищется по этому абсолютному пути, а не относительно текущего
#: рабочего каталога: запуск из корня репозитория, из ``backend/`` и из
#: Docker (WORKDIR /app) иначе молча читают разные (или никакие) файлы
#: окружения — вплоть до «настроенного, но не прочитанного» SMTP.
BACKEND_DIR = Path(__file__).resolve().parents[2]

#: Файлы окружения в порядке приоритета: ``backend/.env`` — база,
#: ``.env`` в CWD (docker-compose, одноразовые запуски) — перекрывает её.
ENV_FILES = (BACKEND_DIR / ".env", ".env")

#: Режим шифрования SMTP-соединения (docs/03 §2).
SmtpSecurity = Literal["starttls", "ssl", "none"]


class Settings(BaseSettings):
    """Конфигурация Career-Assistant-AI (см. backend/.env.example)."""

    model_config = SettingsConfigDict(
        env_file=ENV_FILES,
        env_file_encoding="utf-8",
        extra="ignore",
        # Переменная, заданная пустой строкой, равносильна отсутствующей:
        # ``REFRESH_COOKIE_SECURE=`` в .env.example означает «Secure — авто»
        # (None), а не «строка, которую нельзя разобрать как bool».
        # Без этого приложение падало бы на старте из-за собственного
        # примера окружения.
        env_ignore_empty=True,
    )

    # --- Общее ---
    app_name: str = "Career-Assistant-AI"
    app_version: str = "0.1.0"
    api_prefix: str = "/api/v1"
    debug: bool = False
    #: Окружение: development | staging | production. В production включаются
    #: строгие проверки секретов (fail-fast) и дополнительные security-заголовки.
    environment: str = "development"

    # --- CORS ---
    cors_origins: str = "http://localhost:5500,http://127.0.0.1:5500"
    #: Отправлять ли CORS-credentials (cookies). Wildcard '*' запрещён вместе с ним.
    cors_allow_credentials: bool = True

    # --- Trusted hosts (защита от Host-спуфинга) ---
    trusted_hosts: str = "localhost,127.0.0.1,testserver"

    # --- Security headers ---
    #: Content-Security-Policy.
    #:
    #: Политика уровня «строгий»: в script-src НЕТ 'unsafe-inline' и
    #: 'unsafe-eval', внешние хосты запрещены. Инлайн-скрипты фронтенда
    #: (единственный bootstrap в index.html) получают nonce — его подставляет
    #: nginx ($request_id + sub_filter) либо этот middleware для HTML-ответов.
    #:
    #: Раньше здесь были 'unsafe-inline' и cdn.tailwindcss.com: Play CDN
    #: исполнял JS на клиенте, а тег <style type="text/tailwindcss"> и
    #: Google Fonts требовали внешних хостов в style-src/font-src.
    #: Теперь Tailwind собирается в статический css/styles.min.css (npm run build),
    #: а Inter лежит в frontend/fonts/ — из-за 152-ФЗ ст. 18.1 и GDPR ст. 6/44
    #: передавать IP пользователя на fonts.gstatic.com недопустимо.
    #:
    #: Переопределяется переменной CSP_POLICY, если нужны нестандартные директивы.
    csp_policy: str = (
        "default-src 'self'; "
        # 'strict-dynamic' не используем: приложение не подгружает скрипты
        # динамически, а ES-модули грузятся по 'self' без nonce.
        "script-src 'self' 'nonce-{nonce}'; "
        "style-src 'self'; "
        "img-src 'self' data:; "
        "font-src 'self'; "
        "connect-src 'self' ws: wss:; "
        "object-src 'none'; "
        "frame-ancestors 'none'; "
        "base-uri 'self'; "
        "form-action 'self'; "
        "upgrade-insecure-requests"
    )
    gzip_min_size: int = 1024
    #: Доверять ли X-Forwarded-For / X-Forwarded-Proto (за обратным прокси).
    trust_proxy_headers: bool = False

    # --- Rate limiting (Redis sliding-window) ---
    rate_limit_enabled: bool = True

    # --- PostgreSQL (docs/02_DATABASE.md) ---
    database_url: str = "postgresql+asyncpg://career:career@localhost:5432/career_assistant"

    # --- Redis ---
    redis_url: str = "redis://localhost:6379/0"

    # --- JWT (Auth Module) ---
    jwt_secret: str = "change-me-in-production"
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 30
    refresh_token_expire_days: int = 14

    # --- Refresh-токены и cookies ---
    #: Имя HttpOnly-cookie с refresh-токеном.
    refresh_cookie_name: str = "ca_refresh_token"
    #: Путь cookie (совпадает с api_prefix, чтобы не утекал на статику).
    refresh_cookie_path: str = "/api/v1/auth"
    #: Secure-флаг cookie: None → автоматически (только в production/https).
    refresh_cookie_secure: bool | None = None
    #: SameSite для refresh-cookie: strict | lax | none.
    refresh_cookie_samesite: str = "strict"
    #: Время жизни WS-тикета (одноразовый билет для WebSocket), сек.
    ws_ticket_ttl_seconds: int = 30
    #: Обнаружение повторного использования refresh-токена (revoke all при reuse).
    refresh_token_reuse_detection: bool = True

    # --- SMTP и email-верификация (Auth Module, docs/03 §2) ---
    #: SMTP-сервер исходящих писем. Пусто → отправка невозможна: в dev код
    #: только логируется, в production такой конфиг запрещён (fail-fast ниже).
    smtp_host: str = ""
    #: Порт SMTP: 587 — STARTTLS, 465 — implicit SSL (согласуется с
    #: smtp_security: расхождение — частая причина «письма не уходят»).
    smtp_port: int = 587
    #: Логин SMTP (пусто → анонимное соединение, только для dev-серверов).
    smtp_user: str = ""
    smtp_password: str = ""
    #: Режим шифрования соединения: starttls | ssl | none.
    smtp_security: SmtpSecurity = "starttls"
    #: Отправитель письма: "Имя <адрес>" либо просто адрес.
    emails_from: str = "Career-Assistant-AI <noreply@example.com>"
    #: Срок жизни OTP-кода верификации, минут (ТЗ: 10 минут).
    otp_ttl_minutes: int = 10
    #: Максимум неверных попыток ввода кода (ТЗ: 5; затем блокировка).
    otp_max_attempts: int = 5
    #: Минимальный интервал между отправками кода, сек (resend: 1/60 сек).
    otp_resend_interval_seconds: int = 60

    # --- Queue Manager: Redis + ARQ (docs/01 §3, docs/04 §6) ---
    # Очереди Redis. Задачи попадают в них через enqueue_job() при создании
    # задачи API, а не через опрос таблицы `tasks` (см. queue_manager/queues.py).
    arq_queue_parsing: str = "career:queue:parsing"
    arq_queue_llm: str = "career:queue:llm"
    # Таймаут одной ARQ-задачи, сек (парсинг может идти долго).
    arq_job_timeout_seconds: float = 3600.0
    # Сколько раз ARQ повторит задачу при исключении.
    arq_max_tries: int = 5
    # Глобальный потолок одновременных job'ов в очереди парсинга.
    # Ограничение «≤ 2 на пользователя» (docs/04 §1) держит Redis-семафор.
    arq_parsing_max_jobs: int = 8
    # docs/04 §6: LLM-очередь — строго один воркер на всё приложение.
    arq_llm_max_jobs: int = 1
    # Пауза между проверками очереди Redis, сек (механизм ARQ — ZRANGEBYSCORE
    # по отсортированному множеству; БД при этом не опрашивается вовсе).
    arq_poll_delay_seconds: float = 0.05
    # TTL слота Redis-семафора, сек (защита от «залипших» слотов после падения).
    queue_slot_ttl_seconds: int = 7200
    # Сколько job ждёт свободный слот перед тем, как вернуться в очередь.
    queue_slot_wait_seconds: float = 300.0
    # При старте вернуть в очередь задачи, застрявшие в pending/processing.
    queue_recover_on_startup: bool = True
    #: Redis-блокировка восстановления (SET NX EX), сек. Защищает
    #: `recover_pending_tasks` от параллельного запуска несколькими
    #: репликами API/worker-parsing при старте (production).
    queue_recover_lock_ttl_seconds: int = 300
    # Поднимать ли ARQ-воркеры внутри процесса FastAPI (lifespan).
    # False — воркеры запускаются отдельно (worker_entry.py parsing|llm).
    # В production встроенные воркеры ЗАПРЕЩЕНЫ (см. _validate_security):
    # API и воркеры — независимые рантаймы (worker-parsing ×N, worker-llm ×1).
    queue_embedded_workers: bool = True

    # --- LLM (docs/05_LLM_PIPELINE.md) ---
    ollama_base_url: str = "http://localhost:11434"
    llm_model: str = "qwen2.5:7b"
    llm_timeout_seconds: float = 120.0
    # Максимальный объём compact_resume в символах.
    # Требование задачи: «не более 2000 символов»; docs/05 §3 рекомендует
    # 1800–2200. Лимит настраивается через env (COMPACT_RESUME_MAX_CHARS).
    compact_resume_max_chars: int = 2000

    # --- Прокси / Anti-Ban (docs/04_PARSING_RULES.md §3.1) ---
    # Только резидентные провайдеры (см. RESIDENTIAL_PROVIDERS в anti_ban.proxy);
    # датацентровые запрещены. Пусто + без прокси-списка = direct-режим (dev).
    proxy_provider: str = ""  # напр. brightdata / smartproxy / iproyal
    proxy_gateway: str = ""  # шлюз провайдера host:port (sticky-сессии)
    proxy_username: str = ""
    proxy_password: str = ""
    proxy_list: str = ""  # явные URL через запятую: http://user:pass@host:port

    # --- Observability: логирование (docs/01_ARCHITECTURE.md §9) ---
    # Уровень логгирования корневого логгера и structlog.
    log_level: str = "INFO"
    # JSON-вывод логов (production). В development можно выключить для
    # читаемого console-рендера: LOG_JSON_OUTPUT=false
    log_json_output: bool = True

    # --- Observability: Sentry (docs/01_ARCHITECTURE.md §9) ---
    # Пустой DSN → Sentry выключен (no-op). PII-скраббинг включён всегда:
    # пароли, JWT-токены, raw cookies и ПД кандидата не отправляются.
    sentry_dsn: str = ""
    sentry_traces_sample_rate: float = 0.0
    sentry_send_default_pii: bool = False

    # --- Observability: Prometheus (GET /metrics) ---
    metrics_enabled: bool = True

    # --- Observability: пороги алертов ---
    # Рост LLM-очереди: больше N ожидающих задач → алерт.
    alert_llm_queue_pending_threshold: int = 10
    # Доля ответов с капчей: выше N процентов → алерт.
    alert_captcha_rate_threshold: float = 0.05
    # Доля неуспешных задач: выше N процентов → алерт.
    alert_task_failure_rate_threshold: float = 0.25
    # Сколько секунд недоступности Ollama считать инцидентом.
    alert_ollama_down_seconds: int = 60

    # --- Монетизация: тарифы и квоты (Billing Module, docs/03 §11) ---
    #: Включить контроль суточных квот перед постановкой задач в очередь.
    #: Выключается только для разработки и нагрузотельных прогонов —
    #: в production квоты обязательны (иначе тарифы не имеют смысла).
    billing_enabled: bool = True
    #: Провайдер платёжного шлюза для вебхуков: yookassa | cloudpayments | stripe.
    billing_provider: str = "yookassa"
    #: Секрет подписи вебхуков платёжного шлюза. Пустой в development —
    #: тогда вебхук отклоняется (400), чтобы нельзя было «подменить» оплату.
    billing_webhook_secret: str = ""
    #: Таймаут окна идемпотентности: событие с тем же external id повторно
    #: не обрабатывается, даже если запись была удалена вручную.
    billing_webhook_max_age_seconds: int = 86400
    #: Максимальный размер тела вебхука, байт (защита от DoS разбором JSON).
    billing_webhook_max_body_bytes: int = 262_144

    # --- Учёт прокси-трафика (Proxy Usage Logger, docs/02 §3.11) ---
    #: Стоимость 1 МБ прокси-трафика в копейках — для отчётности по себестоимости.
    proxy_cost_per_mb_kopecks: int = 12
    #: Порог доли капчи по одной задаче, выше которого пишем предупреждение
    #: в лог и поднимаем метрику-алерт (docs/04 §9 — снижаем интенсивность).
    proxy_alert_captcha_rate_threshold: float = 0.05
    #: Порог объёма трафика одной задачи, МБ (аномалия/злоупотребление).
    proxy_alert_mb_per_task_threshold: int = 50
    #: Записывать ли расход прокси в БД (в тестах отключается).
    proxy_usage_persist: bool = True

    @property
    def cors_origin_list(self) -> list[str]:
        """CORS-источники из строки через запятую."""
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @property
    def trusted_host_list(self) -> list[str]:
        """Разрешённые Host-заголовки из строки через запятую."""
        return [host.strip() for host in self.trusted_hosts.split(",") if host.strip()]

    @property
    def is_production(self) -> bool:
        """Production-режим (строгие проверки секретов и hardening)."""
        return self.environment.strip().lower() == "production"

    @property
    def smtp_configured(self) -> bool:
        """Настроен ли исходящий SMTP.

        Без него письма не уходят в принципе: OTP-код существует только в
        письме, поэтому в production такой конфиг запрещён (fail-fast),
        а в development ``send_verification_email`` печатает код в лог.
        """
        return bool(self.smtp_host.strip())

    @property
    def db_echo(self) -> bool:
        """SQL echo: только в debug и НИКОГДА в production (иначе DSN в логах)."""
        return bool(self.debug) and not self.is_production

    @property
    def secure_cookies(self) -> bool:
        """Secure-флаг cookies: явная настройка или автоматически в production."""
        if self.refresh_cookie_secure is not None:
            return self.refresh_cookie_secure
        return self.is_production

    @property
    def effective_embedded_workers(self) -> bool:
        """Флаг встроенных воркеров с учётом окружения.

        В production встроенные воркеры запрещены (docs/01 §6): API и
        воркеры — независимые рантаймы (`worker-parsing` ×N, `worker-llm` ×1),
        поэтому метод всегда возвращает False независимо от значения
        `queue_embedded_workers`.
        """
        if self.is_production:
            return False
        return bool(self.queue_embedded_workers)

    @model_validator(mode="after")
    def _validate_security(self) -> "Settings":
        """Fail-fast проверки безопасности конфигурации.

        - production не должен стартовать с дефолтным/коротким JWT_SECRET;
        - CORS не должен разрешать wildcard '*' вместе с credentials=true;
        - production не должен поднимать встроенные воркеры
          (QUEUE_EMBEDDED_WORKERS=false): API и воркеры — независимые
          рантаймы (worker-parsing ×N, worker-llm строго ×1, docs/01 §6);
        - production обязан иметь SMTP_HOST: OTP-код существует только в
          письме, поэтому без отправки регистрация не завершается никогда.
          Молча поднимать сервис, который не может отправить код, хуже, чем
          не подняться вовсе: сбой виден сразу, а не как «письма не приходят».
        """
        if self.is_production:
            if self.jwt_secret == INSECURE_JWT_SECRET:
                raise ValueError(
                    "ENVIRONMENT=production requires a non-default JWT_SECRET "
                    "(set a strong, random value)"
                )
            if len(self.jwt_secret) < MIN_JWT_SECRET_LENGTH:
                raise ValueError(
                    f"ENVIRONMENT=production requires JWT_SECRET of at least "
                    f"{MIN_JWT_SECRET_LENGTH} characters"
                )
            if self.queue_embedded_workers:
                raise ValueError(
                    "ENVIRONMENT=production requires QUEUE_EMBEDDED_WORKERS=false "
                    "(run workers as separate runtimes: worker-parsing ×N, "
                    "worker-llm strictly ×1)"
                )
            if not self.smtp_configured:
                raise ValueError(
                    "ENVIRONMENT=production requires SMTP_HOST (email OTP codes "
                    "are delivered only by email; set SMTP_HOST/SMTP_PORT/"
                    "SMTP_USER/SMTP_PASSWORD or registration cannot be completed)"
                )

        origins = self.cors_origin_list
        if "*" in origins and self.cors_allow_credentials:
            raise ValueError(
                "CORS wildcard '*' cannot be combined with CORS_ALLOW_CREDENTIALS=true; "
                "list explicit origins instead"
            )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
