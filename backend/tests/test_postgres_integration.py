import asyncio
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from httpx2 import ASGITransport, AsyncClient, Response
from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from app.agents import AgentEngine, GeneratedPlan, MockPlanner, PlannedStep, Planner
from app.core.config import PlannerBackend, Settings
from app.database.session import create_database_engine, create_session_factory
from app.main import create_app
from app.models import (
    Approval,
    ApprovalDecision,
    Execution,
    ExecutionStatus,
    Task,
    TaskStatus,
    ToolCall,
    ToolCallStatus,
)
from app.tools import (
    PermissionLevel,
    ToolDefinition,
    ToolExecutor,
    ToolRegistry,
    WorkspaceListTool,
    WorkspaceMoveTool,
)

pytestmark = pytest.mark.postgres


class PostgreSQLFailurePlanner:
    def create_plan(self, instruction: str) -> GeneratedPlan:
        return GeneratedPlan(
            summary=f'Exercise persisted failure for "{instruction}".',
            steps=(
                PlannedStep(
                    sequence=1,
                    title="Run the PostgreSQL failure tool",
                    tool_name="postgres_failure",
                    arguments={},
                ),
            ),
        )


class PostgreSQLMovePlanner:
    def create_plan(self, instruction: str) -> GeneratedPlan:
        return GeneratedPlan(
            summary=f'Approve a PostgreSQL-backed move for "{instruction}".',
            steps=(
                PlannedStep(
                    sequence=1,
                    title="Move one file after approval",
                    tool_name="workspace_move",
                    arguments={"source": "source.txt", "destination": "moved.txt"},
                ),
            ),
        )


class PostgreSQLFailureTool:
    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="postgres_failure",
            description="Raise an exception to verify PostgreSQL failure persistence.",
            permission=PermissionLevel.READ,
            parameters={},
        )

    def execute(self, arguments: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("PostgreSQL integration failure detail must stay private.")

    def validate_arguments(self, arguments: dict[str, Any]) -> None:
        return None


@pytest.fixture
def postgres_session_factory() -> Iterator[sessionmaker[Session]]:
    database_url = os.getenv("AURA_POSTGRES_TEST_URL")
    if database_url is None:
        pytest.skip("AURA_POSTGRES_TEST_URL is not configured.")
    if not (make_url(database_url).database or "").endswith("_test"):
        pytest.fail("AURA_POSTGRES_TEST_URL must target a database ending in '_test'.")

    engine = create_database_engine(database_url)
    session_factory = create_session_factory(engine)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "TRUNCATE TABLE approvals, tool_calls, executions, plans, tasks "
                    "RESTART IDENTITY CASCADE"
                )
            )
        yield session_factory
    finally:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "TRUNCATE TABLE approvals, tool_calls, executions, plans, tasks "
                    "RESTART IDENTITY CASCADE"
                )
            )
        engine.dispose()


async def request(
    application: FastAPI,
    method: str,
    path: str,
    json: dict[str, str] | None = None,
) -> Response:
    transport = ASGITransport(app=application)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        return await client.request(method, path, json=json)


def build_postgres_application(
    tmp_path: Path,
    session_factory: sessionmaker[Session],
    *,
    failing: bool = False,
    approval: bool = False,
) -> FastAPI:
    registry = ToolRegistry()
    planner: Planner
    if failing:
        registry.register(PostgreSQLFailureTool())
        planner = PostgreSQLFailurePlanner()
    elif approval:
        registry.register(WorkspaceMoveTool(tmp_path))
        planner = PostgreSQLMovePlanner()
    else:
        registry.register(WorkspaceListTool(tmp_path))
        planner = MockPlanner()
    engine = AgentEngine(
        planner,
        ToolExecutor(registry),
        planner_name="postgres-integration-planner",
    )
    return create_app(
        Settings(
            database_url=os.environ["AURA_POSTGRES_TEST_URL"],
            workspace_root=tmp_path,
            planner_backend=PlannerBackend.MOCK,
        ),
        session_factory=session_factory,
        agent_engine=engine,
    )


def test_postgres_create_and_get_round_trip(
    tmp_path: Path,
    postgres_session_factory: sessionmaker[Session],
) -> None:
    (tmp_path / "README.md").write_text("AURA", encoding="utf-8")
    application = build_postgres_application(tmp_path, postgres_session_factory)

    created = asyncio.run(
        request(
            application,
            "POST",
            "/api/tasks",
            json={"instruction": "List this workspace."},
        )
    )
    fetched = asyncio.run(request(application, "GET", f"/api/tasks/{created.json()['id']}"))

    assert created.status_code == 201
    assert created.json()["status"] == "succeeded"
    assert fetched.status_code == 200
    assert fetched.json() == created.json()


def test_postgres_persists_tool_failure_lifecycle(
    tmp_path: Path,
    postgres_session_factory: sessionmaker[Session],
) -> None:
    application = build_postgres_application(tmp_path, postgres_session_factory, failing=True)

    response = asyncio.run(
        request(
            application,
            "POST",
            "/api/tasks",
            json={"instruction": "Persist a safe failure."},
        )
    )

    assert response.status_code == 201
    assert response.json()["status"] == "failed"
    assert response.json()["error"] == 'Tool "postgres_failure" could not be executed safely.'
    with postgres_session_factory() as session:
        task = session.scalar(select(Task))
        execution = session.scalar(select(Execution))
        tool_call = session.scalar(select(ToolCall))
        assert task is not None and task.status is TaskStatus.FAILED
        assert execution is not None and execution.status is ExecutionStatus.FAILED
        assert tool_call is not None and tool_call.status is ToolCallStatus.FAILED


def test_postgres_persists_waiting_and_approved_execution_lifecycle(
    tmp_path: Path,
    postgres_session_factory: sessionmaker[Session],
) -> None:
    (tmp_path / "source.txt").write_text("postgres move", encoding="utf-8")
    application = build_postgres_application(
        tmp_path,
        postgres_session_factory,
        approval=True,
    )

    created = asyncio.run(
        request(
            application,
            "POST",
            "/api/tasks",
            json={"instruction": "Move source.txt to moved.txt."},
        )
    )
    approved = asyncio.run(
        request(
            application,
            "POST",
            f"/api/tasks/{created.json()['id']}/approve",
        )
    )

    assert created.status_code == 201
    assert created.json()["status"] == "waiting_for_approval"
    assert created.json()["execution"]["approval"]["decision"] == "pending"
    assert approved.status_code == 200
    assert approved.json()["status"] == "succeeded"
    with postgres_session_factory() as session:
        task = session.scalar(select(Task))
        execution = session.scalar(select(Execution))
        tool_call = session.scalar(select(ToolCall))
        approval_record = session.scalar(select(Approval))
        assert task is not None and task.status is TaskStatus.SUCCEEDED
        assert execution is not None and execution.status is ExecutionStatus.SUCCEEDED
        assert tool_call is not None and tool_call.status is ToolCallStatus.SUCCEEDED
        assert (
            approval_record is not None
            and approval_record.decision is ApprovalDecision.APPROVED
            and approval_record.decided_at is not None
            and approval_record.filesystem_preconditions is not None
        )
