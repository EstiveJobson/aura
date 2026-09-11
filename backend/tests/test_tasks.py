import asyncio
from pathlib import Path

from fastapi import FastAPI
from httpx2 import ASGITransport, AsyncClient, Response
from sqlalchemy import Engine, create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from app.agents import AgentEngine, ExecutionPlan, PlannedStep
from app.core.config import Settings
from app.database.base import Base
from app.main import create_app
from app.models import Execution, Plan, Task, TaskStatus, ToolCall
from app.tools import ToolExecutor, ToolRegistry, WorkspaceListTool


class MissingToolPlanner:
    def create_plan(self, instruction: str) -> ExecutionPlan:
        return ExecutionPlan(
            planner="failure-test-planner",
            summary=f'Fail safely for "{instruction}".',
            steps=(
                PlannedStep(
                    sequence=1,
                    title="Select an unavailable tool",
                    tool_name="missing",
                    arguments={},
                ),
            ),
        )


def build_test_application(
    tmp_path: Path, agent_engine: AgentEngine | None = None
) -> tuple[FastAPI, sessionmaker[Session], Engine]:
    database_path = tmp_path / "aura-test.db"
    engine = create_engine(
        f"sqlite+pysqlite:///{database_path.as_posix()}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    settings = Settings(
        database_url=f"sqlite+pysqlite:///{database_path.as_posix()}",
        workspace_root=tmp_path,
    )
    application = create_app(
        settings,
        session_factory=session_factory,
        agent_engine=agent_engine,
    )
    return application, session_factory, engine


async def request(
    application: FastAPI, method: str, path: str, json: dict[str, str] | None = None
) -> Response:
    transport = ASGITransport(app=application)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        return await client.request(method, path, json=json)


def test_create_task_runs_and_persists_the_complete_vertical_slice(tmp_path: Path) -> None:
    (tmp_path / "backend").mkdir()
    (tmp_path / "README.md").write_text("AURA", encoding="utf-8")
    application, session_factory, engine = build_test_application(tmp_path)

    response = asyncio.run(
        request(
            application,
            "POST",
            "/api/tasks",
            json={"instruction": "  List this workspace.  "},
        )
    )

    assert response.status_code == 201
    body = response.json()
    assert body["instruction"] == "List this workspace."
    assert body["status"] == "succeeded"
    assert body["result"] == "Found 3 top-level entries in the configured workspace."
    assert body["plan"]["planner"] == "mock-planner-v1"
    assert body["plan"]["steps"] == [
        {
            "sequence": 1,
            "title": "List top-level workspace entries",
            "tool_name": "workspace_list",
            "arguments": {},
        }
    ]
    assert body["execution"]["status"] == "succeeded"
    assert len(body["execution"]["tool_calls"]) == 1
    assert body["execution"]["tool_calls"][0]["tool_name"] == "workspace_list"
    assert body["execution"]["tool_calls"][0]["status"] == "succeeded"

    task_id = body["id"]
    persisted = asyncio.run(request(application, "GET", f"/api/tasks/{task_id}"))
    assert persisted.status_code == 200
    persisted_body = persisted.json()
    assert persisted_body["id"] == body["id"]
    assert persisted_body["status"] == "succeeded"
    assert persisted_body["plan"]["id"] == body["plan"]["id"]
    assert persisted_body["execution"]["id"] == body["execution"]["id"]
    assert (
        persisted_body["execution"]["tool_calls"][0]["id"]
        == body["execution"]["tool_calls"][0]["id"]
    )

    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Task)) == 1
        assert session.scalar(select(func.count()).select_from(Plan)) == 1
        assert session.scalar(select(func.count()).select_from(Execution)) == 1
        assert session.scalar(select(func.count()).select_from(ToolCall)) == 1
        task = session.scalar(select(Task))
        assert task is not None
        assert task.status is TaskStatus.SUCCEEDED
    engine.dispose()


def test_tool_failure_is_persisted_and_returned_as_operational_status(tmp_path: Path) -> None:
    registry = ToolRegistry()
    registry.register(WorkspaceListTool(tmp_path))
    failing_engine = AgentEngine(MissingToolPlanner(), ToolExecutor(registry))
    application, session_factory, engine = build_test_application(tmp_path, failing_engine)

    response = asyncio.run(
        request(application, "POST", "/api/tasks", json={"instruction": "Fail safely."})
    )

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "failed"
    assert body["error"] == 'Tool "missing" is not registered.'
    assert body["execution"]["status"] == "failed"
    assert body["execution"]["tool_calls"][0]["status"] == "failed"
    with session_factory() as session:
        task = session.scalar(select(Task))
        assert task is not None
        assert task.status is TaskStatus.FAILED
    engine.dispose()


def test_task_api_validates_input_and_returns_a_typed_not_found_error(tmp_path: Path) -> None:
    application, _, engine = build_test_application(tmp_path)

    invalid = asyncio.run(request(application, "POST", "/api/tasks", json={"instruction": "   "}))
    missing = asyncio.run(
        request(
            application,
            "GET",
            "/api/tasks/efdbf199-3630-484c-8dfb-49b18d0ae32f",
        )
    )

    assert invalid.status_code == 422
    assert missing.status_code == 404
    assert missing.json() == {
        "code": "task_not_found",
        "message": "Task was not found.",
    }
    engine.dispose()
