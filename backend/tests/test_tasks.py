import asyncio
import logging
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from httpx2 import ASGITransport, AsyncClient, Response
from sqlalchemy import Engine, create_engine, func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.agents import AgentEngine, GeneratedPlan, PlannedStep
from app.core.config import Settings
from app.database.base import Base
from app.main import create_app
from app.models import Execution, ExecutionStatus, Plan, Task, TaskStatus, ToolCall, ToolCallStatus
from app.services.tasks import PERSISTED_FAILURE_MESSAGE
from app.tools import (
    PermissionLevel,
    ToolDefinition,
    ToolExecutor,
    ToolRegistry,
    WorkspaceListTool,
)


class MissingToolPlanner:
    def create_plan(self, instruction: str) -> GeneratedPlan:
        return GeneratedPlan(
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


class PlannerFailure:
    def create_plan(self, instruction: str) -> GeneratedPlan:
        raise RuntimeError("raw provider payload with api_key=do-not-log")


class ExplodingToolPlanner:
    def create_plan(self, instruction: str) -> GeneratedPlan:
        return GeneratedPlan(
            summary=f'Exercise the tool boundary for "{instruction}".',
            steps=(
                PlannedStep(
                    sequence=1,
                    title="Run a registered failing tool",
                    tool_name="exploding_tool",
                    arguments={},
                ),
            ),
        )


class ExplodingTool:
    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="exploding_tool",
            description="Raise an internal exception for boundary testing.",
            permission=PermissionLevel.READ,
            parameters={},
        )

    def execute(self, arguments: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("credential=do-not-log")


class CompletionCommitFailureSession(Session):
    _commit_attempts = 0

    def commit(self) -> None:
        self._commit_attempts += 1
        if self._commit_attempts == 4:
            self.flush()
            raise SQLAlchemyError("database details with password=do-not-log")
        super().commit()


class PlanFlushFailureSession(Session):
    _flush_attempts = 0

    def flush(self, objects: Sequence[Any] | None = None) -> None:
        self._flush_attempts += 1
        if self._flush_attempts == 3:
            raise SQLAlchemyError("database details with password=do-not-log")
        super().flush(objects)


def build_test_application(
    tmp_path: Path,
    agent_engine: AgentEngine | None = None,
    session_class: type[Session] = Session,
) -> tuple[FastAPI, sessionmaker[Session], Engine]:
    database_path = tmp_path / "aura-test.db"
    engine = create_engine(
        f"sqlite+pysqlite:///{database_path.as_posix()}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(
        bind=engine,
        class_=session_class,
        autoflush=False,
        expire_on_commit=False,
    )
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


def test_unregistered_tool_plan_is_rejected_before_execution_persistence(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    registry = ToolRegistry()
    registry.register(WorkspaceListTool(tmp_path))
    failing_engine = AgentEngine(
        MissingToolPlanner(),
        ToolExecutor(registry),
        planner_name="failure-test-planner",
    )
    application, session_factory, engine = build_test_application(tmp_path, failing_engine)

    with caplog.at_level(logging.WARNING, logger="app.services.tasks"):
        response = asyncio.run(
            request(application, "POST", "/api/tasks", json={"instruction": "Fail safely."})
        )

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "failed"
    assert body["error"] == "The generated plan was rejected."
    assert body["plan"] is None
    assert body["execution"] is None
    with session_factory() as session:
        task = session.scalar(select(Task))
        assert task is not None
        assert task.status is TaskStatus.FAILED
        assert session.scalar(select(func.count()).select_from(Plan)) == 0
        assert session.scalar(select(func.count()).select_from(Execution)) == 0
    failure_records = [
        record
        for record in caplog.records
        if record.getMessage().startswith("task execution failed")
    ]
    assert len(failure_records) == 1
    assert getattr(failure_records[0], "failure_stage", None) == "plan_validation"
    engine.dispose()


def test_planner_failure_is_sanitized_persisted_and_logged_once(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    registry = ToolRegistry()
    registry.register(WorkspaceListTool(tmp_path))
    failing_engine = AgentEngine(
        PlannerFailure(),
        ToolExecutor(registry),
        planner_name="failure-test-planner",
    )
    application, _, engine = build_test_application(tmp_path, failing_engine)

    with caplog.at_level(logging.WARNING, logger="app.services.tasks"):
        response = asyncio.run(
            request(application, "POST", "/api/tasks", json={"instruction": "Fail planning."})
        )

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "failed"
    assert body["error"] == "The deterministic planner could not create a plan."
    assert body["plan"] is None
    assert body["execution"] is None
    failure_records = [
        record
        for record in caplog.records
        if record.getMessage().startswith("task execution failed")
    ]
    assert len(failure_records) == 1
    assert getattr(failure_records[0], "failure_stage", None) == "planner"
    assert "api_key" not in caplog.text
    engine.dispose()


def test_actual_tool_exception_is_sanitized_persisted_and_logged_once(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    registry = ToolRegistry()
    registry.register(ExplodingTool())
    failing_engine = AgentEngine(
        ExplodingToolPlanner(),
        ToolExecutor(registry),
        planner_name="failure-test-planner",
    )
    application, session_factory, engine = build_test_application(tmp_path, failing_engine)

    with caplog.at_level(logging.WARNING, logger="app.services.tasks"):
        response = asyncio.run(
            request(application, "POST", "/api/tasks", json={"instruction": "Fail safely."})
        )

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "failed"
    assert body["error"] == 'Tool "exploding_tool" could not be executed safely.'
    assert body["plan"]["planner"] == "failure-test-planner"
    assert body["execution"]["status"] == "failed"
    assert body["execution"]["tool_calls"][0]["status"] == "failed"
    with session_factory() as session:
        task = session.scalar(select(Task))
        execution = session.scalar(select(Execution))
        tool_call = session.scalar(select(ToolCall))
        assert task is not None and task.status is TaskStatus.FAILED
        assert execution is not None and execution.status is ExecutionStatus.FAILED
        assert tool_call is not None and tool_call.status is ToolCallStatus.FAILED
    failure_records = [
        record
        for record in caplog.records
        if record.getMessage().startswith("task execution failed")
    ]
    assert len(failure_records) == 1
    assert getattr(failure_records[0], "failure_stage", None) == "tool"
    assert "credential" not in caplog.text
    engine.dispose()


def test_completion_commit_failure_returns_infrastructure_error_and_recovers_state(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    application, session_factory, engine = build_test_application(
        tmp_path,
        session_class=CompletionCommitFailureSession,
    )

    with caplog.at_level(logging.ERROR, logger="app.services.tasks"):
        response = asyncio.run(
            request(application, "POST", "/api/tasks", json={"instruction": "List files."})
        )

    assert response.status_code == 503
    assert response.json() == {
        "code": "task_persistence_failed",
        "message": "The task could not be persisted because storage is unavailable.",
    }
    with session_factory() as session:
        task = session.scalar(select(Task))
        execution = session.scalar(select(Execution))
        tool_call = session.scalar(select(ToolCall))
        assert task is not None and task.status is TaskStatus.FAILED
        assert task.result is None
        assert task.error == PERSISTED_FAILURE_MESSAGE
        assert execution is not None and execution.status is ExecutionStatus.FAILED
        assert execution.result is None
        assert tool_call is not None and tool_call.status is ToolCallStatus.FAILED
        assert tool_call.result is None
    persistence_records = [
        record
        for record in caplog.records
        if record.getMessage().startswith("task persistence write failed")
    ]
    assert len(persistence_records) == 1
    assert getattr(persistence_records[0], "persistence_operation", None) == "complete_execution"
    assert getattr(persistence_records[0], "task_id", None)
    assert getattr(persistence_records[0], "execution_id", None)
    assert getattr(persistence_records[0], "tool_call_id", None)
    assert getattr(persistence_records[0], "rollback_succeeded", None) is True
    assert getattr(persistence_records[0], "failure_state_persisted", None) is True
    assert "password" not in caplog.text
    engine.dispose()


def test_plan_flush_failure_rolls_back_and_marks_existing_task_failed(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    application, session_factory, engine = build_test_application(
        tmp_path,
        session_class=PlanFlushFailureSession,
    )

    with caplog.at_level(logging.ERROR, logger="app.services.tasks"):
        response = asyncio.run(
            request(application, "POST", "/api/tasks", json={"instruction": "List files."})
        )

    assert response.status_code == 503
    with session_factory() as session:
        task = session.scalar(select(Task))
        assert task is not None and task.status is TaskStatus.FAILED
        assert task.error == PERSISTED_FAILURE_MESSAGE
        assert session.scalar(select(func.count()).select_from(Plan)) == 0
        assert session.scalar(select(func.count()).select_from(Execution)) == 0
        assert session.scalar(select(func.count()).select_from(ToolCall)) == 0
    persistence_records = [
        record
        for record in caplog.records
        if record.getMessage().startswith("task persistence write failed")
    ]
    assert len(persistence_records) == 1
    assert getattr(persistence_records[0], "persistence_operation", None) == "prepare_execution"
    assert getattr(persistence_records[0], "failure_state_persisted", None) is True
    assert "password" not in caplog.text
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
