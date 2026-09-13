import asyncio
import logging
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from httpx2 import ASGITransport, AsyncClient, Response
from sqlalchemy import Engine, create_engine, func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.agents import AgentEngine, GeneratedPlan, LLMPlanner, PlannedStep
from app.core.config import PlannerBackend, Settings
from app.database.base import Base
from app.main import create_app
from app.models import (
    Approval,
    ApprovalDecision,
    Execution,
    ExecutionStatus,
    Plan,
    Task,
    TaskStatus,
    ToolCall,
    ToolCallStatus,
)
from app.providers import ProviderTimeoutError, StructuredGenerationRequest
from app.services import TaskService
from app.services.tasks import (
    PERSISTED_FAILURE_MESSAGE,
    UNCERTAIN_OUTCOME_MESSAGE,
)
from app.tools import (
    PermissionLevel,
    ToolDefinition,
    ToolExecutor,
    ToolRegistry,
    WorkspaceListTool,
    WorkspaceMoveTool,
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


class PlannerFailureProvider:
    def generate(self, request: StructuredGenerationRequest) -> dict[str, Any]:
        raise ProviderTimeoutError("raw provider payload with api_key=do-not-log")


class InvalidArgumentsPlanner:
    def create_plan(self, instruction: str) -> GeneratedPlan:
        return GeneratedPlan(
            summary="Reject arbitrary path arguments.",
            steps=(
                PlannedStep(
                    sequence=1,
                    title="Attempt an invalid path",
                    tool_name="workspace_list",
                    arguments={"path": "../outside"},
                ),
            ),
        )


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


class MovePlanner:
    def __init__(self, source: str = "source.txt", destination: str = "moved.txt") -> None:
        self._source = source
        self._destination = destination

    def create_plan(self, instruction: str) -> GeneratedPlan:
        return GeneratedPlan(
            summary=f'Propose a bounded move for "{instruction}".',
            steps=(
                PlannedStep(
                    sequence=1,
                    title="Move one workspace file",
                    tool_name="workspace_move",
                    arguments={
                        "source": self._source,
                        "destination": self._destination,
                    },
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

    def validate_arguments(self, arguments: dict[str, Any]) -> None:
        return None


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


class WriteCompletionCommitFailureSession(Session):
    armed = False

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._commit_attempts = 0

    def commit(self) -> None:
        self._commit_attempts += 1
        if type(self).armed and self._commit_attempts == 2:
            self.flush()
            raise SQLAlchemyError("database details with password=do-not-log")
        super().commit()


class CountingWorkspaceMoveTool(WorkspaceMoveTool):
    def __init__(self, workspace_root: Path) -> None:
        super().__init__(workspace_root)
        self.invocations = 0

    def execute(
        self,
        arguments: dict[str, Any],
        *,
        approval_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.invocations += 1
        return super().execute(arguments, approval_context=approval_context)


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
        planner_backend=PlannerBackend.MOCK,
    )
    application = create_app(
        settings,
        session_factory=session_factory,
        agent_engine=agent_engine,
    )
    return application, session_factory, engine


async def request(
    application: FastAPI,
    method: str,
    path: str,
    json: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> Response:
    request_headers = headers
    if request_headers is None and path.endswith(("/approve", "/reject")):
        request_headers = {"X-AURA-Decision": "approve" if path.endswith("/approve") else "reject"}
    transport = ASGITransport(app=application)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        return await client.request(method, path, json=json, headers=request_headers)


async def run_application_lifespan(application: FastAPI) -> None:
    async with application.router.lifespan_context(application):
        pass


def build_move_engine(
    tmp_path: Path,
    move_tool: WorkspaceMoveTool | None = None,
) -> AgentEngine:
    registry = ToolRegistry()
    registry.register(move_tool or WorkspaceMoveTool(tmp_path))
    return AgentEngine(
        MovePlanner(),
        ToolExecutor(registry),
        planner_name="move-test-planner",
    )


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
        LLMPlanner(PlannerFailureProvider(), registry.definitions()),
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
    assert body["error"] == "The planner could not create a plan."
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


def test_invalid_tool_arguments_are_rejected_before_plan_persistence(tmp_path: Path) -> None:
    registry = ToolRegistry()
    registry.register(WorkspaceListTool(tmp_path))
    failing_engine = AgentEngine(
        InvalidArgumentsPlanner(),
        ToolExecutor(registry),
        planner_name="failure-test-planner",
    )
    application, session_factory, engine = build_test_application(tmp_path, failing_engine)

    response = asyncio.run(
        request(application, "POST", "/api/tasks", json={"instruction": "List elsewhere."})
    )

    assert response.status_code == 201
    assert response.json()["status"] == "failed"
    assert response.json()["error"] == "The generated plan was rejected."
    assert response.json()["plan"] is None
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Plan)) == 0
        assert session.scalar(select(func.count()).select_from(Execution)) == 0
        assert session.scalar(select(func.count()).select_from(ToolCall)) == 0
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


def test_mutating_task_waits_for_persisted_approval_then_executes_once(
    tmp_path: Path,
) -> None:
    (tmp_path / "source.txt").write_text("move me", encoding="utf-8")
    application, session_factory, engine = build_test_application(
        tmp_path,
        build_move_engine(tmp_path),
    )

    created = asyncio.run(
        request(
            application,
            "POST",
            "/api/tasks",
            json={"instruction": "Move source.txt to moved.txt."},
        )
    )

    assert created.status_code == 201
    waiting = created.json()
    assert waiting["status"] == "waiting_for_approval"
    assert waiting["execution"]["status"] == "waiting_for_approval"
    assert waiting["execution"]["tool_calls"][0]["status"] == "waiting_for_approval"
    assert waiting["execution"]["approval"]["decision"] == "pending"
    assert "filesystem_preconditions" not in waiting["execution"]["approval"]
    assert (tmp_path / "source.txt").exists()
    assert not (tmp_path / "moved.txt").exists()

    approved = asyncio.run(
        request(
            application,
            "POST",
            f"/api/tasks/{waiting['id']}/approve",
            json={"source": "untrusted.txt", "destination": "attacker.txt"},
        )
    )

    assert approved.status_code == 200
    completed = approved.json()
    assert completed["status"] == "succeeded"
    assert completed["execution"]["status"] == "succeeded"
    assert completed["execution"]["approval"]["decision"] == "approved"
    assert completed["execution"]["approval"]["decided_at"] is not None
    assert completed["execution"]["tool_calls"][0]["status"] == "succeeded"
    assert not (tmp_path / "source.txt").exists()
    assert (tmp_path / "moved.txt").read_text(encoding="utf-8") == "move me"
    assert not (tmp_path / "attacker.txt").exists()

    duplicate = asyncio.run(request(application, "POST", f"/api/tasks/{waiting['id']}/approve"))
    assert duplicate.status_code == 409
    assert duplicate.json() == {
        "code": "task_state_conflict",
        "message": "Task is not waiting for approval.",
    }
    with session_factory() as session:
        task = session.scalar(select(Task))
        execution = session.scalar(select(Execution))
        tool_call = session.scalar(select(ToolCall))
        approval = session.scalar(select(Approval))
        assert task is not None and task.status is TaskStatus.SUCCEEDED
        assert execution is not None and execution.status is ExecutionStatus.SUCCEEDED
        assert tool_call is not None and tool_call.status is ToolCallStatus.SUCCEEDED
        assert approval is not None and approval.decision is ApprovalDecision.APPROVED
        assert approval.filesystem_preconditions is not None
        assert set(approval.filesystem_preconditions) == {"version", "workspace", "source"}
    engine.dispose()


def test_reject_persists_terminal_state_without_mutating_workspace(tmp_path: Path) -> None:
    (tmp_path / "source.txt").write_text("keep me", encoding="utf-8")
    application, session_factory, engine = build_test_application(
        tmp_path,
        build_move_engine(tmp_path),
    )
    created = asyncio.run(
        request(
            application,
            "POST",
            "/api/tasks",
            json={"instruction": "Move source.txt to moved.txt."},
        )
    ).json()

    rejected = asyncio.run(request(application, "POST", f"/api/tasks/{created['id']}/reject"))

    assert rejected.status_code == 200
    body = rejected.json()
    assert body["status"] == "rejected"
    assert body["execution"]["status"] == "rejected"
    assert body["execution"]["tool_calls"][0]["status"] == "rejected"
    assert body["execution"]["approval"]["decision"] == "rejected"
    assert (tmp_path / "source.txt").read_text(encoding="utf-8") == "keep me"
    assert not (tmp_path / "moved.txt").exists()

    duplicate = asyncio.run(request(application, "POST", f"/api/tasks/{created['id']}/approve"))
    assert duplicate.status_code == 409
    with session_factory() as session:
        assert session.scalar(select(Task)).status is TaskStatus.REJECTED  # type: ignore[union-attr]
        assert session.scalar(select(Execution)).status is ExecutionStatus.REJECTED  # type: ignore[union-attr]
        assert session.scalar(select(ToolCall)).status is ToolCallStatus.REJECTED  # type: ignore[union-attr]
        assert session.scalar(select(Approval)).decision is ApprovalDecision.REJECTED  # type: ignore[union-attr]
    engine.dispose()


def test_approval_revalidates_destination_and_persists_safe_failure(tmp_path: Path) -> None:
    (tmp_path / "source.txt").write_text("source", encoding="utf-8")
    application, _, engine = build_test_application(tmp_path, build_move_engine(tmp_path))
    created = asyncio.run(
        request(
            application,
            "POST",
            "/api/tasks",
            json={"instruction": "Move source.txt to moved.txt."},
        )
    ).json()
    (tmp_path / "moved.txt").write_text("new current state", encoding="utf-8")

    response = asyncio.run(request(application, "POST", f"/api/tasks/{created['id']}/approve"))

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "failed"
    assert body["execution"]["approval"]["decision"] == "approved"
    assert body["error"] == 'Tool "workspace_move" could not be executed safely.'
    assert (tmp_path / "source.txt").read_text(encoding="utf-8") == "source"
    assert (tmp_path / "moved.txt").read_text(encoding="utf-8") == "new current state"
    engine.dispose()


def test_approval_rejects_a_replaced_source_and_requires_a_fresh_task(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.txt"
    source.write_text("approved source", encoding="utf-8")
    application, _, engine = build_test_application(tmp_path, build_move_engine(tmp_path))
    created = asyncio.run(
        request(
            application,
            "POST",
            "/api/tasks",
            json={"instruction": "Move source.txt to moved.txt."},
        )
    ).json()
    replacement = tmp_path / "replacement.txt"
    replacement.write_text("replacement", encoding="utf-8")
    os.replace(replacement, source)

    approved = asyncio.run(request(application, "POST", f"/api/tasks/{created['id']}/approve"))
    duplicate = asyncio.run(request(application, "POST", f"/api/tasks/{created['id']}/approve"))

    assert approved.status_code == 200
    assert approved.json()["status"] == "failed"
    assert approved.json()["execution"]["approval"]["decision"] == "approved"
    assert approved.json()["error"] == 'Tool "workspace_move" could not be executed safely.'
    assert duplicate.status_code == 409
    assert source.read_text(encoding="utf-8") == "replacement"
    assert not (tmp_path / "moved.txt").exists()
    engine.dispose()


@pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="Linux permits replacing the configured root while its descriptor remains open.",
)
def test_approval_rejects_a_changed_workspace_identity(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "source.txt").write_text("approved source", encoding="utf-8")
    application, _, engine = build_test_application(tmp_path, build_move_engine(workspace))
    created = asyncio.run(
        request(
            application,
            "POST",
            "/api/tasks",
            json={"instruction": "Move source.txt to moved.txt."},
        )
    ).json()

    detached_workspace = tmp_path / "detached-workspace"
    workspace.rename(detached_workspace)
    workspace.mkdir()
    (workspace / "source.txt").write_text("different workspace", encoding="utf-8")

    approved = asyncio.run(request(application, "POST", f"/api/tasks/{created['id']}/approve"))
    duplicate = asyncio.run(request(application, "POST", f"/api/tasks/{created['id']}/approve"))

    assert approved.status_code == 200
    assert approved.json()["status"] == "failed"
    assert duplicate.status_code == 409
    assert (detached_workspace / "source.txt").read_text(encoding="utf-8") == ("approved source")
    assert (workspace / "source.txt").read_text(encoding="utf-8") == "different workspace"
    assert not (detached_workspace / "moved.txt").exists()
    assert not (workspace / "moved.txt").exists()
    engine.dispose()


def test_post_mutation_completion_failure_persists_uncertainty_without_replay(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.txt"
    source.write_text("move exactly once", encoding="utf-8")
    move_tool = CountingWorkspaceMoveTool(tmp_path)
    application, session_factory, engine = build_test_application(
        tmp_path,
        build_move_engine(tmp_path, move_tool),
        session_class=WriteCompletionCommitFailureSession,
    )
    created = asyncio.run(
        request(
            application,
            "POST",
            "/api/tasks",
            json={"instruction": "Move source.txt to moved.txt."},
        )
    ).json()

    WriteCompletionCommitFailureSession.armed = True
    try:
        response = asyncio.run(request(application, "POST", f"/api/tasks/{created['id']}/approve"))
    finally:
        WriteCompletionCommitFailureSession.armed = False

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "outcome_uncertain"
    assert body["error"] == UNCERTAIN_OUTCOME_MESSAGE
    assert body["execution"]["status"] == "outcome_uncertain"
    assert body["execution"]["tool_calls"][0]["status"] == "outcome_uncertain"
    assert body["execution"]["approval"]["decision"] == "approved"
    assert not source.exists()
    assert (tmp_path / "moved.txt").read_text(encoding="utf-8") == "move exactly once"
    assert move_tool.invocations == 1

    fetched = asyncio.run(request(application, "GET", f"/api/tasks/{created['id']}"))
    duplicate = asyncio.run(request(application, "POST", f"/api/tasks/{created['id']}/approve"))
    with session_factory() as session:
        reconciled = TaskService(
            session,
            application.state.agent_engine,
        ).reconcile_stranded_executions()

    assert fetched.status_code == 200
    assert fetched.json()["status"] == "outcome_uncertain"
    assert duplicate.status_code == 409
    assert reconciled == 0
    assert move_tool.invocations == 1
    engine.dispose()


def test_stranded_approved_write_reconciliation_is_terminal_and_never_replays(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.txt"
    source.write_text("still here", encoding="utf-8")
    move_tool = CountingWorkspaceMoveTool(tmp_path)
    agent_engine = build_move_engine(tmp_path, move_tool)
    application, session_factory, engine = build_test_application(tmp_path, agent_engine)
    created = asyncio.run(
        request(
            application,
            "POST",
            "/api/tasks",
            json={"instruction": "Move source.txt to moved.txt."},
        )
    )
    assert created.status_code == 201

    with session_factory() as session:
        task = session.scalar(select(Task))
        execution = session.scalar(select(Execution))
        tool_call = session.scalar(select(ToolCall))
        approval = session.scalar(select(Approval))
        assert task is not None
        assert execution is not None
        assert tool_call is not None
        assert approval is not None
        task.status = TaskStatus.EXECUTING
        execution.status = ExecutionStatus.RUNNING
        tool_call.status = ToolCallStatus.RUNNING
        approval.decision = ApprovalDecision.APPROVED
        approval.decided_at = task.updated_at
        session.commit()

    asyncio.run(run_application_lifespan(application))
    with session_factory() as session:
        assert TaskService(session, agent_engine).reconcile_stranded_executions() == 0

    with session_factory() as session:
        task = session.scalar(select(Task))
        execution = session.scalar(select(Execution))
        tool_call = session.scalar(select(ToolCall))
        approval = session.scalar(select(Approval))
        assert task is not None and task.status is TaskStatus.OUTCOME_UNCERTAIN
        assert execution is not None and execution.status is ExecutionStatus.OUTCOME_UNCERTAIN
        assert tool_call is not None and tool_call.status is ToolCallStatus.OUTCOME_UNCERTAIN
        assert approval is not None and approval.decision is ApprovalDecision.APPROVED
        assert task.error == UNCERTAIN_OUTCOME_MESSAGE
    assert source.read_text(encoding="utf-8") == "still here"
    assert not (tmp_path / "moved.txt").exists()
    assert move_tool.invocations == 0
    engine.dispose()


@pytest.mark.parametrize("decision", ["approve", "reject"])
def test_decision_requests_require_trusted_browser_origin_and_custom_header(
    tmp_path: Path,
    decision: str,
) -> None:
    (tmp_path / "source.txt").write_text("browser decision", encoding="utf-8")
    application, _, engine = build_test_application(tmp_path, build_move_engine(tmp_path))
    created = asyncio.run(
        request(
            application,
            "POST",
            "/api/tasks",
            json={"instruction": "Move source.txt to moved.txt."},
        )
    ).json()
    decision_path = f"/api/tasks/{created['id']}/{decision}"

    untrusted = asyncio.run(
        request(
            application,
            "POST",
            decision_path,
            headers={
                "Origin": "https://attacker.example",
                "X-AURA-Decision": decision,
            },
        )
    )
    missing = asyncio.run(
        request(
            application,
            "POST",
            decision_path,
            headers={"Origin": "http://localhost:5173"},
        )
    )
    invalid = asyncio.run(
        request(
            application,
            "POST",
            decision_path,
            headers={
                "Origin": "http://localhost:5173",
                "X-AURA-Decision": "reject" if decision == "approve" else "approve",
            },
        )
    )
    preflight = asyncio.run(
        request(
            application,
            "OPTIONS",
            decision_path,
            headers={
                "Origin": "http://localhost:5173",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "X-AURA-Decision",
            },
        )
    )
    trusted = asyncio.run(
        request(
            application,
            "POST",
            decision_path,
            headers={
                "Origin": "http://localhost:5173",
                "X-AURA-Decision": decision,
            },
        )
    )

    assert untrusted.status_code == 403
    assert untrusted.json()["code"] == "decision_origin_rejected"
    assert missing.status_code == 400
    assert missing.json()["code"] == "decision_header_invalid"
    assert invalid.status_code == 400
    assert invalid.json()["code"] == "decision_header_invalid"
    assert preflight.status_code == 200
    assert preflight.headers["access-control-allow-origin"] == "http://localhost:5173"
    assert "x-aura-decision" in preflight.headers["access-control-allow-headers"].lower()
    assert trusted.status_code == 200
    assert trusted.headers["access-control-allow-origin"] == "http://localhost:5173"
    expected_status = "succeeded" if decision == "approve" else "rejected"
    assert trusted.json()["status"] == expected_status
    engine.dispose()


def test_approval_endpoints_reject_missing_and_non_waiting_tasks(tmp_path: Path) -> None:
    application, _, engine = build_test_application(tmp_path)
    completed = asyncio.run(
        request(
            application,
            "POST",
            "/api/tasks",
            json={"instruction": "List this workspace."},
        )
    ).json()

    missing = asyncio.run(
        request(
            application,
            "POST",
            "/api/tasks/efdbf199-3630-484c-8dfb-49b18d0ae32f/approve",
        )
    )
    completed_conflict = asyncio.run(
        request(application, "POST", f"/api/tasks/{completed['id']}/reject")
    )

    assert missing.status_code == 404
    assert missing.json()["code"] == "task_not_found"
    assert completed_conflict.status_code == 409
    assert completed_conflict.json()["code"] == "task_state_conflict"
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
