from sqlalchemy import text

from app.database.base import Base
from app.database.session import create_database_engine, create_session_factory
from app.models import Execution, Plan, Task, ToolCall  # noqa: F401


def test_database_foundation_builds_a_working_session() -> None:
    engine = create_database_engine("sqlite+pysqlite:///:memory:")
    session_factory = create_session_factory(engine)

    try:
        with session_factory() as session:
            assert session.scalar(text("SELECT 1")) == 1
    finally:
        engine.dispose()

    assert set(Base.metadata.tables) == {"tasks", "plans", "executions", "tool_calls"}
