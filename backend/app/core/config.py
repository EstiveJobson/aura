from enum import StrEnum
from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[3]


class PlannerBackend(StrEnum):
    MOCK = "mock"
    OPENAI = "openai"


class WorkspaceWriteMode(StrEnum):
    READ_ONLY = "read_only"
    DOCKER_MANAGED = "docker_managed"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "AURA API"
    environment: str = "development"
    api_prefix: str = "/api"
    database_url: str = "postgresql+psycopg://aura:aura@localhost:5432/aura"
    cors_origins: list[str] = ["http://localhost:5173"]
    workspace_root: Path = PROJECT_ROOT / "workspace"
    workspace_write_mode: WorkspaceWriteMode = WorkspaceWriteMode.READ_ONLY
    planner_backend: PlannerBackend = PlannerBackend.MOCK
    openai_api_key: SecretStr | None = None
    openai_model: str = Field(default="gpt-5-mini", min_length=1, max_length=40)
    ai_provider_timeout_seconds: float = Field(default=20, ge=1, le=60)
    ai_provider_max_output_tokens: int = Field(default=512, ge=128, le=2_048)

    @model_validator(mode="after")
    def validate_provider_configuration(self) -> "Settings":
        if self.planner_backend is PlannerBackend.OPENAI and (
            self.openai_api_key is None or not self.openai_api_key.get_secret_value().strip()
        ):
            raise ValueError("OPENAI_API_KEY is required when PLANNER_BACKEND=openai.")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
