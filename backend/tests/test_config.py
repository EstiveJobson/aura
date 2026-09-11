from app.core.config import Settings, get_settings


def test_settings_accept_foundation_configuration() -> None:
    settings = Settings(
        api_prefix="/api",
        database_url="postgresql+psycopg://aura:aura@localhost:5432/aura",
        cors_origins=["http://localhost:5173"],
    )

    assert settings.api_prefix == "/api"
    assert settings.cors_origins == ["http://localhost:5173"]
    assert settings.database_url.startswith("postgresql+psycopg://")


def test_settings_cache_returns_same_instance() -> None:
    get_settings.cache_clear()

    assert get_settings() is get_settings()
