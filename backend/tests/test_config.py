import pytest
from pydantic import SecretStr, ValidationError

from app.core.config import PROJECT_ROOT, PlannerBackend, Settings, get_settings


def test_settings_accept_foundation_configuration() -> None:
    settings = Settings(
        api_prefix="/api",
        database_url="postgresql+psycopg://aura:aura@localhost:5432/aura",
        cors_origins=["http://localhost:5173"],
    )

    assert settings.api_prefix == "/api"
    assert settings.cors_origins == ["http://localhost:5173"]
    assert settings.database_url.startswith("postgresql+psycopg://")
    assert settings.planner_backend is PlannerBackend.MOCK


def test_openai_configuration_requires_a_key_without_exposing_secrets() -> None:
    with pytest.raises(ValidationError, match="OPENAI_API_KEY is required"):
        Settings(
            planner_backend=PlannerBackend.OPENAI,
            openai_api_key=SecretStr(""),
        )

    settings = Settings(
        planner_backend=PlannerBackend.OPENAI,
        openai_api_key=SecretStr("not-a-real-secret"),
    )
    assert "not-a-real-secret" not in repr(settings)

    with pytest.raises(ValidationError) as error:
        Settings(
            planner_backend=PlannerBackend.OPENAI,
            openai_api_key=SecretStr("another-not-real-secret"),
            ai_provider_timeout_seconds=61,
        )
    assert "another-not-real-secret" not in str(error.value)


def test_settings_load_the_documented_root_environment_file() -> None:
    assert Settings.model_config["env_file"] == PROJECT_ROOT / ".env"


def test_settings_cache_returns_same_instance() -> None:
    get_settings.cache_clear()

    assert get_settings() is get_settings()
