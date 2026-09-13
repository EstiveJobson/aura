import asyncio
import os
import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, ClassVar, cast
from uuid import UUID

import pytest
from fastapi import FastAPI
from httpx2 import ASGITransport, AsyncClient, Response
from sqlalchemy import select, text
from sqlalchemy.engine import Connection, Engine, make_url
from sqlalchemy.exc import SQLAlchemyError
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
from app.services import TaskService
from app.services.tasks import (
    WRITE_COMPLETION_UNCERTAIN,
    RecoveryPolicy,
    RecoveryResult,
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


class CountingWorkspaceMoveTool(WorkspaceMoveTool):
    def __init__(self, workspace_root: Path) -> None:
        super().__init__(workspace_root)
        self._invocations = 0
        self._lock = threading.Lock()

    @property
    def invocations(self) -> int:
        with self._lock:
            return self._invocations

    def execute(
        self,
        arguments: dict[str, Any],
        *,
        approval_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            self._invocations += 1
        return super().execute(arguments, approval_context=approval_context)


class FailNextCommitSession(Session):
    _lock: ClassVar[threading.Lock] = threading.Lock()
    _armed: ClassVar[bool] = False
    _claimed: ClassVar[bool] = False

    @classmethod
    def arm(cls) -> None:
        with cls._lock:
            cls._armed = True
            cls._claimed = False

    @classmethod
    def disarm(cls) -> None:
        with cls._lock:
            cls._armed = False

    def commit(self) -> None:
        should_fail = False
        with type(self)._lock:
            if type(self)._armed and not type(self)._claimed:
                type(self)._claimed = True
                should_fail = True
        if should_fail:
            self.flush()
            raise SQLAlchemyError("injected decision commit failure")
        super().commit()


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
    headers: dict[str, str] | None = None,
) -> Response:
    request_headers = headers
    if request_headers is None and path.endswith(("/approve", "/reject")):
        request_headers = {"X-AURA-Decision": "approve" if path.endswith("/approve") else "reject"}
    transport = ASGITransport(app=application)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        return await client.request(method, path, json=json, headers=request_headers)


def build_postgres_application(
    tmp_path: Path,
    session_factory: sessionmaker[Session],
    *,
    failing: bool = False,
    approval: bool = False,
    move_tool: WorkspaceMoveTool | None = None,
) -> FastAPI:
    registry = ToolRegistry()
    planner: Planner
    if failing:
        registry.register(PostgreSQLFailureTool())
        planner = PostgreSQLFailurePlanner()
    elif approval:
        registry.register(move_tool or WorkspaceMoveTool(tmp_path))
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


def _concurrent_decisions(
    application: FastAPI,
    paths: tuple[str, str],
) -> tuple[Response, Response]:
    barrier = threading.Barrier(3)

    def send(path: str) -> Response:
        barrier.wait(timeout=10)
        return asyncio.run(request(application, "POST", path))

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(send, paths[0])
        second = executor.submit(send, paths[1])
        barrier.wait(timeout=10)
        return first.result(timeout=20), second.result(timeout=20)


def test_postgres_concurrent_approve_approve_invokes_write_exactly_once(
    tmp_path: Path,
    postgres_session_factory: sessionmaker[Session],
) -> None:
    (tmp_path / "source.txt").write_text("one invocation", encoding="utf-8")
    move_tool = CountingWorkspaceMoveTool(tmp_path)
    application = build_postgres_application(
        tmp_path,
        postgres_session_factory,
        approval=True,
        move_tool=move_tool,
    )
    created = asyncio.run(
        request(
            application,
            "POST",
            "/api/tasks",
            json={"instruction": "Move source.txt to moved.txt."},
        )
    ).json()
    approve_path = f"/api/tasks/{created['id']}/approve"

    first, second = _concurrent_decisions(application, (approve_path, approve_path))

    assert sorted((first.status_code, second.status_code)) == [200, 409]
    assert move_tool.invocations == 1
    assert not (tmp_path / "source.txt").exists()
    assert (tmp_path / "moved.txt").read_text(encoding="utf-8") == "one invocation"
    with postgres_session_factory() as session:
        assert session.scalar(select(Task)).status is TaskStatus.SUCCEEDED  # type: ignore[union-attr]
        assert session.scalar(select(Approval)).decision is ApprovalDecision.APPROVED  # type: ignore[union-attr]


def test_postgres_concurrent_approve_reject_has_one_consistent_winner(
    tmp_path: Path,
    postgres_session_factory: sessionmaker[Session],
) -> None:
    source = tmp_path / "source.txt"
    source.write_text("one decision", encoding="utf-8")
    move_tool = CountingWorkspaceMoveTool(tmp_path)
    application = build_postgres_application(
        tmp_path,
        postgres_session_factory,
        approval=True,
        move_tool=move_tool,
    )
    created = asyncio.run(
        request(
            application,
            "POST",
            "/api/tasks",
            json={"instruction": "Move source.txt to moved.txt."},
        )
    ).json()
    base_path = f"/api/tasks/{created['id']}"

    approve, reject = _concurrent_decisions(
        application,
        (f"{base_path}/approve", f"{base_path}/reject"),
    )

    assert sorted((approve.status_code, reject.status_code)) == [200, 409]
    with postgres_session_factory() as session:
        task = session.scalar(select(Task))
        execution = session.scalar(select(Execution))
        tool_call = session.scalar(select(ToolCall))
        approval = session.scalar(select(Approval))
        assert task is not None
        assert execution is not None
        assert tool_call is not None
        assert approval is not None
        if approval.decision is ApprovalDecision.APPROVED:
            assert task.status is TaskStatus.SUCCEEDED
            assert execution.status is ExecutionStatus.SUCCEEDED
            assert tool_call.status is ToolCallStatus.SUCCEEDED
            assert move_tool.invocations == 1
            assert not source.exists()
            assert (tmp_path / "moved.txt").exists()
        else:
            assert approval.decision is ApprovalDecision.REJECTED
            assert task.status is TaskStatus.REJECTED
            assert execution.status is ExecutionStatus.REJECTED
            assert tool_call.status is ToolCallStatus.REJECTED
            assert move_tool.invocations == 0
            assert source.exists()
            assert not (tmp_path / "moved.txt").exists()


def test_postgres_failed_decision_recovery_cannot_overwrite_newer_success(
    tmp_path: Path,
    postgres_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "source.txt").write_text("newer decision wins", encoding="utf-8")
    move_tool = CountingWorkspaceMoveTool(tmp_path)
    database_engine = cast(Engine, postgres_session_factory.kw["bind"])
    failing_factory = sessionmaker(
        bind=database_engine,
        class_=FailNextCommitSession,
        autoflush=False,
        expire_on_commit=False,
    )
    application = build_postgres_application(
        tmp_path,
        cast(sessionmaker[Session], failing_factory),
        approval=True,
        move_tool=move_tool,
    )
    created = asyncio.run(
        request(
            application,
            "POST",
            "/api/tasks",
            json={"instruction": "Move source.txt to moved.txt."},
        )
    ).json()
    approve_path = f"/api/tasks/{created['id']}/approve"
    recovery_entered = threading.Event()
    successful_decision_finished = threading.Event()
    recovery_results: list[RecoveryResult] = []
    original_recovery = TaskService._recover_failed_lifecycle

    def delayed_recovery(
        bind: Engine | Connection,
        task_id: UUID,
        execution_id: UUID | None,
        tool_call_id: UUID | None,
        policy: RecoveryPolicy,
    ) -> RecoveryResult:
        recovery_entered.set()
        if not successful_decision_finished.wait(timeout=10):
            raise RuntimeError("successful decision did not finish")
        result = original_recovery(
            bind,
            task_id,
            execution_id,
            tool_call_id,
            policy,
        )
        recovery_results.append(result)
        return result

    monkeypatch.setattr(
        TaskService,
        "_recover_failed_lifecycle",
        staticmethod(delayed_recovery),
    )
    FailNextCommitSession.arm()
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            failed_future = executor.submit(
                lambda: asyncio.run(request(application, "POST", approve_path))
            )
            assert recovery_entered.wait(timeout=10)
            try:
                successful = asyncio.run(request(application, "POST", approve_path))
            finally:
                successful_decision_finished.set()
            failed = failed_future.result(timeout=20)
    finally:
        FailNextCommitSession.disarm()

    assert failed.status_code == 503
    assert successful.status_code == 200
    assert recovery_results == [RecoveryResult.NOT_APPLIED]
    assert move_tool.invocations == 1
    with postgres_session_factory() as session:
        assert session.scalar(select(Task)).status is TaskStatus.SUCCEEDED  # type: ignore[union-attr]
        assert session.scalar(select(Execution)).status is ExecutionStatus.SUCCEEDED  # type: ignore[union-attr]
        assert session.scalar(select(ToolCall)).status is ToolCallStatus.SUCCEEDED  # type: ignore[union-attr]
        assert session.scalar(select(Approval)).decision is ApprovalDecision.APPROVED  # type: ignore[union-attr]


def test_postgres_recovery_refuses_to_overwrite_terminal_lifecycle(
    tmp_path: Path,
    postgres_session_factory: sessionmaker[Session],
) -> None:
    (tmp_path / "source.txt").write_text("terminal", encoding="utf-8")
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
    ).json()
    completed = asyncio.run(
        request(application, "POST", f"/api/tasks/{created['id']}/approve")
    ).json()
    database_engine = cast(Engine, postgres_session_factory.kw["bind"])

    recovery = TaskService._recover_failed_lifecycle(
        database_engine,
        UUID(created["id"]),
        UUID(completed["execution"]["id"]),
        UUID(completed["execution"]["tool_calls"][0]["id"]),
        WRITE_COMPLETION_UNCERTAIN,
    )

    assert recovery is RecoveryResult.NOT_APPLIED
    with postgres_session_factory() as session:
        assert session.scalar(select(Task)).status is TaskStatus.SUCCEEDED  # type: ignore[union-attr]
        assert session.scalar(select(Execution)).status is ExecutionStatus.SUCCEEDED  # type: ignore[union-attr]
        assert session.scalar(select(ToolCall)).status is ToolCallStatus.SUCCEEDED  # type: ignore[union-attr]
        assert session.scalar(select(Approval)).decision is ApprovalDecision.APPROVED  # type: ignore[union-attr]
