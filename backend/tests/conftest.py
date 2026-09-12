from collections.abc import Iterator

import pytest

from app.core.config import get_settings


@pytest.fixture(autouse=True)
def force_deterministic_planner(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Guarantee the normal test suite cannot select or call a real provider."""
    monkeypatch.setenv("PLANNER_BACKEND", "mock")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
