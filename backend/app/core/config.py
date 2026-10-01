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

    @property
    def cors_origin_list(self) -> list[str]:
        """CORS-источники из строки через запятую."""
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
