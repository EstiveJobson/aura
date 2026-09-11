from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env", "../.env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "AURA API"
    environment: str = "development"
    api_prefix: str = "/api"
    database_url: str = "postgresql+psycopg://aura:aura@localhost:5432/aura"
    cors_origins: list[str] = ["http://localhost:5173"]
    workspace_root: Path = Path(".")


@lru_cache
def get_settings() -> Settings:
    return Settings()
