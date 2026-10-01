"""Настройки приложения (переменные окружения / .env)."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Конфигурация Career-Assistant-AI (см. backend/.env.example)."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- Общее ---
    app_name: str = "Career-Assistant-AI"
    app_version: str = "0.1.0"
    api_prefix: str = "/api/v1"
    debug: bool = False

    # --- CORS ---
    cors_origins: str = "http://localhost:5500,http://127.0.0.1:5500"

    # --- PostgreSQL (docs/02_DATABASE.md) ---
    database_url: str = "postgresql+asyncpg://career:career@localhost:5432/career_assistant"

    # --- Redis ---
    redis_url: str = "redis://localhost:6379/0"

    # --- JWT (Auth Module) ---
    jwt_secret: str = "change-me-in-production"
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 30
    refresh_token_expire_days: int = 14

    # --- LLM (docs/05_LLM_PIPELINE.md) ---
    ollama_base_url: str = "http://localhost:11434"
    llm_model: str = "qwen2.5:7b"
    llm_timeout_seconds: float = 120.0
    # Максимальный объём compact_resume (TASK: «не более 1000 символов»;
    # docs/05 §3 рекомендует 1800–2200 — лимит настраивается через env).
    compact_resume_max_chars: int = 1000

    # --- Прокси / Anti-Ban (docs/04_PARSING_RULES.md §3.1) ---
    # Только резидентные провайдеры (см. RESIDENTIAL_PROVIDERS в anti_ban.proxy);
    # датацентровые запрещены. Пусто + без прокси-списка = direct-режим (dev).
    proxy_provider: str = ""  # напр. brightdata / smartproxy / iproyal
    proxy_gateway: str = ""  # шлюз провайдера host:port (sticky-сессии)
    proxy_username: str = ""
    proxy_password: str = ""
    proxy_list: str = ""  # явные URL через запятую: http://user:pass@host:port

    @property
    def cors_origin_list(self) -> list[str]:
        """CORS-источники из строки через запятую."""
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
