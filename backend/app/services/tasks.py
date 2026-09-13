import logging
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, selectinload

from app.agents.engine import AgentEngine, AgentExecutionError
from app.agents.planner import ExecutionPlan
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
from app.models.execution import utc_now

logger = logging.getLogger(__name__)

PERSISTENCE_FAILURE_MESSAGE = "The task could not be persisted because storage is unavailable."
PERSISTED_FAILURE_MESSAGE = "Task execution failed because its state could not be persisted."
UNCERTAIN_OUTCOME_MESSAGE = (
    "The approved write may have changed the workspace, but its durable completion state "
    "is unknown. It will not be run again automatically."
)
INTERRUPTED_EXECUTION_MESSAGE = (
    "Task execution was interrupted before a durable outcome was recorded."
)
TASK_NOT_FOUND_MESSAGE = "Task was not found."
TASK_STATE_CONFLICT_MESSAGE = "Task is not waiting for approval."
STRANDED_RECONCILIATION_LIMIT = 100


class RecoveryTarget(StrEnum):
    FAILED = "failed"
    OUTCOME_UNCERTAIN = "outcome_uncertain"
    PRESERVE = "preserve"


class RecoveryResult(StrEnum):
    NOT_APPLIED = "not_applied"
    FAILED = "failed"
    OUTCOME_UNCERTAIN = "outcome_uncertain"
    PRESERVED = "preserved"


@dataclass(frozen=True)
class RecoveryPolicy:
    expected_task_status: TaskStatus
    expected_execution_status: ExecutionStatus | None
    expected_tool_call_status: ToolCallStatus | None
    expected_approval_decision: ApprovalDecision | None
    target: RecoveryTarget


PENDING_TASK_FAILURE = RecoveryPolicy(
    TaskStatus.PENDING,
    None,
    None,
    None,
    RecoveryTarget.FAILED,
)
PLANNING_TASK_FAILURE = RecoveryPolicy(
    TaskStatus.PLANNING,
    None,
    None,
    None,
    RecoveryTarget.FAILED,
)
WAITING_DECISION_PRESERVE = RecoveryPolicy(
    TaskStatus.WAITING_FOR_APPROVAL,
    ExecutionStatus.WAITING_FOR_APPROVAL,
    ToolCallStatus.WAITING_FOR_APPROVAL,
    ApprovalDecision.PENDING,
    RecoveryTarget.PRESERVE,
)
READ_EXECUTION_FAILURE = RecoveryPolicy(
    TaskStatus.EXECUTING,
    ExecutionStatus.RUNNING,
    ToolCallStatus.RUNNING,
    None,
    RecoveryTarget.FAILED,
)
WRITE_EXECUTION_FAILURE = RecoveryPolicy(
    TaskStatus.EXECUTING,
    ExecutionStatus.RUNNING,
    ToolCallStatus.RUNNING,
    ApprovalDecision.APPROVED,
    RecoveryTarget.FAILED,
)
WRITE_COMPLETION_UNCERTAIN = RecoveryPolicy(
    TaskStatus.EXECUTING,
    ExecutionStatus.RUNNING,
    ToolCallStatus.RUNNING,
    ApprovalDecision.APPROVED,
    RecoveryTarget.OUTCOME_UNCERTAIN,
)


TASK_TRANSITIONS: dict[TaskStatus, frozenset[TaskStatus]] = {
    TaskStatus.PENDING: frozenset({TaskStatus.PLANNING, TaskStatus.FAILED}),
    TaskStatus.PLANNING: frozenset(
        {
            TaskStatus.WAITING_FOR_APPROVAL,
            TaskStatus.EXECUTING,
            TaskStatus.FAILED,
        }
    ),
    TaskStatus.WAITING_FOR_APPROVAL: frozenset(
        {TaskStatus.EXECUTING, TaskStatus.REJECTED, TaskStatus.FAILED}
    ),
    TaskStatus.EXECUTING: frozenset(
        {TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.OUTCOME_UNCERTAIN}
    ),
    TaskStatus.SUCCEEDED: frozenset(),
    TaskStatus.REJECTED: frozenset(),
    TaskStatus.FAILED: frozenset(),
    TaskStatus.OUTCOME_UNCERTAIN: frozenset(),
}
EXECUTION_TRANSITIONS: dict[ExecutionStatus, frozenset[ExecutionStatus]] = {
    ExecutionStatus.WAITING_FOR_APPROVAL: frozenset(
        {ExecutionStatus.RUNNING, ExecutionStatus.REJECTED, ExecutionStatus.FAILED}
    ),
    ExecutionStatus.RUNNING: frozenset(
        {
            ExecutionStatus.SUCCEEDED,
            ExecutionStatus.FAILED,
            ExecutionStatus.OUTCOME_UNCERTAIN,
        }
    ),
    ExecutionStatus.SUCCEEDED: frozenset(),
    ExecutionStatus.REJECTED: frozenset(),
    ExecutionStatus.FAILED: frozenset(),
    ExecutionStatus.OUTCOME_UNCERTAIN: frozenset(),
}
TOOL_CALL_TRANSITIONS: dict[ToolCallStatus, frozenset[ToolCallStatus]] = {
    ToolCallStatus.WAITING_FOR_APPROVAL: frozenset(
        {ToolCallStatus.RUNNING, ToolCallStatus.REJECTED, ToolCallStatus.FAILED}
    ),
    ToolCallStatus.RUNNING: frozenset(
        {
            ToolCallStatus.SUCCEEDED,
            ToolCallStatus.FAILED,
            ToolCallStatus.OUTCOME_UNCERTAIN,
        }
    ),
    ToolCallStatus.SUCCEEDED: frozenset(),
    ToolCallStatus.REJECTED: frozenset(),
    ToolCallStatus.FAILED: frozenset(),
    ToolCallStatus.OUTCOME_UNCERTAIN: frozenset(),
}
APPROVAL_TRANSITIONS: dict[ApprovalDecision, frozenset[ApprovalDecision]] = {
    ApprovalDecision.PENDING: frozenset({ApprovalDecision.APPROVED, ApprovalDecision.REJECTED}),
    ApprovalDecision.APPROVED: frozenset(),
    ApprovalDecision.REJECTED: frozenset(),
}


class TaskPersistenceError(RuntimeError):
    """A predictable, sanitized infrastructure failure for the API boundary."""

    code = "task_persistence_failed"

    def __init__(self) -> None:
        super().__init__(PERSISTENCE_FAILURE_MESSAGE)
        self.message = PERSISTENCE_FAILURE_MESSAGE


class TaskNotFoundError(RuntimeError):
    code = "task_not_found"

    def __init__(self) -> None:
        super().__init__(TASK_NOT_FOUND_MESSAGE)
        self.message = TASK_NOT_FOUND_MESSAGE


class TaskStateConflictError(RuntimeError):
    code = "task_state_conflict"

    def __init__(self) -> None:
        super().__init__(TASK_STATE_CONFLICT_MESSAGE)
        self.message = TASK_STATE_CONFLICT_MESSAGE


class _TaskOutcomeUncertainError(RuntimeError):
    """Internal signal that uncertainty was persisted after a completion write failed."""


def _transition_task(task: Task, target: TaskStatus) -> None:
    if target not in TASK_TRANSITIONS[task.status]:
        raise RuntimeError(f"Invalid task transition: {task.status} -> {target}.")
    task.status = target


def _transition_execution(execution: Execution, target: ExecutionStatus) -> None:
    if target not in EXECUTION_TRANSITIONS[execution.status]:
        raise RuntimeError(f"Invalid execution transition: {execution.status} -> {target}.")
    execution.status = target


def _transition_tool_call(tool_call: ToolCall, target: ToolCallStatus) -> None:
    if target not in TOOL_CALL_TRANSITIONS[tool_call.status]:
        raise RuntimeError(f"Invalid tool-call transition: {tool_call.status} -> {target}.")
    tool_call.status = target


def _transition_approval(approval: Approval, target: ApprovalDecision) -> None:
    if target not in APPROVAL_TRANSITIONS[approval.decision]:
        raise RuntimeError(f"Invalid approval transition: {approval.decision} -> {target}.")
    approval.decision = target


class TaskService:
    def __init__(self, session: Session, agent_engine: AgentEngine) -> None:
        self._session = session
        self._agent_engine = agent_engine

    def create_and_execute(self, instruction: str) -> Task:
        task = Task(id=uuid4(), instruction=instruction, status=TaskStatus.PENDING)
        self._session.add(task)
        self._commit_or_raise(
            "create_task",
            task,
            recovery_policy=PENDING_TASK_FAILURE,
        )

        plan_record: Plan | None = None
        execution: Execution | None = None
        tool_call: ToolCall | None = None
        try:
            _transition_task(task, TaskStatus.PLANNING)
            self._commit_or_raise(
                "start_planning",
                task,
                recovery_policy=PENDING_TASK_FAILURE,
            )

            plan = self._agent_engine.create_plan(instruction)
            plan_record = Plan(
                task=task,
                planner=plan.planner,
                summary=plan.summary,
                steps=[step.model_dump(mode="json") for step in plan.steps],
            )
            requires_approval = self._agent_engine.requires_approval(plan)
            approval_context = (
                self._agent_engine.capture_approval_context(plan) if requires_approval else None
            )
            execution_status = (
                ExecutionStatus.WAITING_FOR_APPROVAL
                if requires_approval
                else ExecutionStatus.RUNNING
            )
            tool_call_status = (
                ToolCallStatus.WAITING_FOR_APPROVAL if requires_approval else ToolCallStatus.RUNNING
            )
            execution = Execution(task=task, status=execution_status)
            self._session.add_all((plan_record, execution))
            self._flush_or_raise(
                "prepare_execution",
                task,
                execution,
                recovery_policy=PLANNING_TASK_FAILURE,
            )

            step = plan.steps[0]
            tool_call = ToolCall(
                execution=execution,
                plan_id=plan_record.id,
                tool_name=step.tool_name,
                arguments=step.arguments,
                status=tool_call_status,
            )
            self._session.add(tool_call)
            if requires_approval:
                _transition_task(task, TaskStatus.WAITING_FOR_APPROVAL)
                self._session.add(
                    Approval(
                        execution=execution,
                        decision=ApprovalDecision.PENDING,
                        filesystem_preconditions=approval_context,
                    )
                )
                self._commit_or_raise(
                    "await_approval",
                    task,
                    execution,
                    tool_call,
                    recovery_policy=PLANNING_TASK_FAILURE,
                )
            else:
                _transition_task(task, TaskStatus.EXECUTING)
                self._commit_or_raise(
                    "start_execution",
                    task,
                    execution,
                    tool_call,
                    recovery_policy=PLANNING_TASK_FAILURE,
                )

                outcome = self._agent_engine.execute(plan)
                self._complete_execution(
                    task,
                    execution,
                    tool_call,
                    outcome.output,
                    outcome.final_result,
                    uncertain_on_persistence_failure=False,
                )
        except AgentExecutionError as exc:
            self._log_execution_failure(exc, task, execution, tool_call)
            self._mark_execution_failed(task, execution, tool_call, str(exc))

        return self._reload_created_task(task, execution, tool_call)

    def approve(self, task_id: UUID) -> Task:
        task = self._get_for_decision(task_id)
        execution, tool_call, approval = self._require_waiting_for_approval(task)
        plan = self._execution_plan(task)
        step = plan.steps[0]
        if tool_call.tool_name != step.tool_name or tool_call.arguments != step.arguments:
            raise TaskStateConflictError

        decided_at = utc_now()
        _transition_approval(approval, ApprovalDecision.APPROVED)
        approval.decided_at = decided_at
        _transition_task(task, TaskStatus.EXECUTING)
        _transition_execution(execution, ExecutionStatus.RUNNING)
        _transition_tool_call(tool_call, ToolCallStatus.RUNNING)
        self._commit_or_raise(
            "approve_execution",
            task,
            execution,
            tool_call,
            recovery_policy=WAITING_DECISION_PRESERVE,
        )

        try:
            outcome = self._agent_engine.execute(
                plan,
                approved=True,
                approval_context=approval.filesystem_preconditions,
            )
            self._complete_execution(
                task,
                execution,
                tool_call,
                outcome.output,
                outcome.final_result,
                uncertain_on_persistence_failure=True,
            )
        except _TaskOutcomeUncertainError:
            return self._reload_created_task(task, execution, tool_call)
        except AgentExecutionError as exc:
            self._log_execution_failure(exc, task, execution, tool_call)
            self._mark_execution_failed(task, execution, tool_call, str(exc))
        return self._reload_created_task(task, execution, tool_call)

    def reject(self, task_id: UUID) -> Task:
        task = self._get_for_decision(task_id)
        execution, tool_call, approval = self._require_waiting_for_approval(task)
        completed_at = utc_now()
        _transition_approval(approval, ApprovalDecision.REJECTED)
        approval.decided_at = completed_at
        _transition_task(task, TaskStatus.REJECTED)
        task.result = None
        task.error = None
        _transition_execution(execution, ExecutionStatus.REJECTED)
        execution.result = None
        execution.error = None
        execution.completed_at = completed_at
        _transition_tool_call(tool_call, ToolCallStatus.REJECTED)
        tool_call.result = None
        tool_call.error = None
        tool_call.completed_at = completed_at
        self._commit_or_raise(
            "reject_execution",
            task,
            execution,
            tool_call,
            recovery_policy=WAITING_DECISION_PRESERVE,
        )
        return self._reload_created_task(task, execution, tool_call)

    def _reload_created_task(
        self,
        task: Task,
        execution: Execution | None,
        tool_call: ToolCall | None,
    ) -> Task:
        self._session.expire_all()
        persisted_task = self.get(task.id)
        if persisted_task is None:
            logger.error(
                "created task could not be reloaded task_id=%s",
                task.id,
                extra={
                    "task_id": str(task.id),
                    "execution_id": str(execution.id) if execution is not None else None,
                    "tool_call_id": str(tool_call.id) if tool_call is not None else None,
                    "persistence_operation": "reload_created_task",
                    "database_error_type": None,
                    "rollback_succeeded": self._rollback(),
                    "failure_state_persisted": False,
                },
            )
            raise TaskPersistenceError
        return persisted_task

    def get(self, task_id: UUID) -> Task | None:
        statement = (
            select(Task)
            .where(Task.id == task_id)
            .options(
                selectinload(Task.plan),
                selectinload(Task.execution).selectinload(Execution.tool_calls),
                selectinload(Task.execution).selectinload(Execution.approval),
            )
        )
        try:
            return self._session.scalar(statement)
        except SQLAlchemyError as exc:
            rollback_succeeded = self._rollback()
            logger.error(
                "task persistence read failed task_id=%s error_type=%s",
                task_id,
                type(exc).__name__,
                extra={
                    "task_id": str(task_id),
                    "execution_id": None,
                    "tool_call_id": None,
                    "persistence_operation": "read_task",
                    "database_error_type": type(exc).__name__,
                    "rollback_succeeded": rollback_succeeded,
                    "failure_state_persisted": False,
                },
            )
            raise TaskPersistenceError from exc

    def _get_for_decision(self, task_id: UUID) -> Task:
        statement = (
            select(Task)
            .where(Task.id == task_id)
            .options(
                selectinload(Task.plan),
                selectinload(Task.execution).selectinload(Execution.tool_calls),
                selectinload(Task.execution).selectinload(Execution.approval),
            )
            .with_for_update()
        )
        try:
            task = self._session.scalar(statement)
        except SQLAlchemyError as exc:
            rollback_succeeded = self._rollback()
            logger.error(
                "approval state read failed task_id=%s error_type=%s",
                task_id,
                type(exc).__name__,
                extra={
                    "task_id": str(task_id),
                    "execution_id": None,
                    "tool_call_id": None,
                    "persistence_operation": "read_approval_state",
                    "database_error_type": type(exc).__name__,
                    "rollback_succeeded": rollback_succeeded,
                    "failure_state_persisted": False,
                },
            )
            raise TaskPersistenceError from exc
        if task is None:
            raise TaskNotFoundError
        return task

    @staticmethod
    def _require_waiting_for_approval(
        task: Task,
    ) -> tuple[Execution, ToolCall, Approval]:
        execution = task.execution
        if (
            task.status is not TaskStatus.WAITING_FOR_APPROVAL
            or task.plan is None
            or execution is None
            or execution.status is not ExecutionStatus.WAITING_FOR_APPROVAL
            or execution.approval is None
            or execution.approval.decision is not ApprovalDecision.PENDING
            or len(execution.tool_calls) != 1
            or execution.tool_calls[0].status is not ToolCallStatus.WAITING_FOR_APPROVAL
        ):
            raise TaskStateConflictError
        return execution, execution.tool_calls[0], execution.approval

    @staticmethod
    def _execution_plan(task: Task) -> ExecutionPlan:
        if task.plan is None:
            raise TaskStateConflictError
        try:
            return ExecutionPlan.model_validate(
                {
                    "planner": task.plan.planner,
                    "summary": task.plan.summary,
                    "steps": task.plan.steps,
                }
            )
        except ValidationError as exc:
            raise TaskStateConflictError from exc

    def _complete_execution(
        self,
        task: Task,
        execution: Execution,
        tool_call: ToolCall,
        output: dict[str, Any],
        final_result: str,
        *,
        uncertain_on_persistence_failure: bool,
    ) -> None:
        completed_at = utc_now()
        _transition_tool_call(tool_call, ToolCallStatus.SUCCEEDED)
        tool_call.result = output
        tool_call.error = None
        tool_call.completed_at = completed_at
        _transition_execution(execution, ExecutionStatus.SUCCEEDED)
        execution.result = output
        execution.error = None
        execution.completed_at = completed_at
        _transition_task(task, TaskStatus.SUCCEEDED)
        task.result = final_result
        task.error = None
        recovery_policy = (
            WRITE_COMPLETION_UNCERTAIN
            if uncertain_on_persistence_failure
            else READ_EXECUTION_FAILURE
        )
        self._commit_or_raise(
            "complete_execution",
            task,
            execution,
            tool_call,
            recovery_policy=recovery_policy,
        )

    def _mark_execution_failed(
        self,
        task: Task,
        execution: Execution | None,
        tool_call: ToolCall | None,
        error: str,
    ) -> None:
        completed_at = utc_now()
        if tool_call is not None:
            _transition_tool_call(tool_call, ToolCallStatus.FAILED)
            tool_call.result = None
            tool_call.error = error
            tool_call.completed_at = completed_at
        if execution is not None:
            _transition_execution(execution, ExecutionStatus.FAILED)
            execution.result = None
            execution.error = error
            execution.completed_at = completed_at
        _transition_task(task, TaskStatus.FAILED)
        task.result = None
        task.error = error
        if execution is None:
            recovery_policy = PLANNING_TASK_FAILURE
        elif execution.approval is None:
            recovery_policy = READ_EXECUTION_FAILURE
        else:
            recovery_policy = WRITE_EXECUTION_FAILURE
        self._commit_or_raise(
            "record_execution_failure",
            task,
            execution,
            tool_call,
            recovery_policy=recovery_policy,
        )

    def _commit_or_raise(
        self,
        operation: str,
        task: Task,
        execution: Execution | None = None,
        tool_call: ToolCall | None = None,
        *,
        recovery_policy: RecoveryPolicy | None = None,
    ) -> None:
        task_id = task.id
        execution_id = execution.id if execution is not None else None
        tool_call_id = tool_call.id if tool_call is not None else None
        try:
            self._session.commit()
        except SQLAlchemyError as exc:
            self._handle_write_failure(
                operation,
                exc,
                task_id,
                execution_id,
                tool_call_id,
                recovery_policy,
            )

    def _flush_or_raise(
        self,
        operation: str,
        task: Task,
        execution: Execution | None = None,
        tool_call: ToolCall | None = None,
        *,
        recovery_policy: RecoveryPolicy | None = None,
    ) -> None:
        task_id = task.id
        execution_id = execution.id if execution is not None else None
        tool_call_id = tool_call.id if tool_call is not None else None
        try:
            self._session.flush()
        except SQLAlchemyError as exc:
            self._handle_write_failure(
                operation,
                exc,
                task_id,
                execution_id,
                tool_call_id,
                recovery_policy,
            )

    def _handle_write_failure(
        self,
        operation: str,
        error: SQLAlchemyError,
        task_id: UUID,
        execution_id: UUID | None,
        tool_call_id: UUID | None,
        recovery_policy: RecoveryPolicy | None,
    ) -> None:
        bind = self._session.get_bind()
        rollback_succeeded = self._rollback()
        failure_state_persisted = False
        recovery_result = RecoveryResult.NOT_APPLIED
        recovery_error_type: str | None = None

        if rollback_succeeded and recovery_policy is not None:
            try:
                recovery_result = self._recover_failed_lifecycle(
                    bind,
                    task_id,
                    execution_id,
                    tool_call_id,
                    recovery_policy,
                )
                failure_state_persisted = recovery_result in {
                    RecoveryResult.FAILED,
                    RecoveryResult.OUTCOME_UNCERTAIN,
                }
            except SQLAlchemyError as recovery_error:
                recovery_error_type = type(recovery_error).__name__

        logger.error(
            (
                "task persistence write failed task_id=%s execution_id=%s "
                "tool_call_id=%s operation=%s error_type=%s rollback_succeeded=%s "
                "failure_state_persisted=%s recovery_result=%s recovery_error_type=%s"
            ),
            task_id,
            execution_id,
            tool_call_id,
            operation,
            type(error).__name__,
            rollback_succeeded,
            failure_state_persisted,
            recovery_result,
            recovery_error_type,
            extra={
                "task_id": str(task_id),
                "execution_id": str(execution_id) if execution_id is not None else None,
                "tool_call_id": str(tool_call_id) if tool_call_id is not None else None,
                "persistence_operation": operation,
                "database_error_type": type(error).__name__,
                "rollback_succeeded": rollback_succeeded,
                "failure_state_persisted": failure_state_persisted,
                "recovery_result": recovery_result,
                "recovery_error_type": recovery_error_type,
            },
        )
        if recovery_result is RecoveryResult.OUTCOME_UNCERTAIN:
            raise _TaskOutcomeUncertainError from error
        raise TaskPersistenceError from error

    @staticmethod
    def _recover_failed_lifecycle(
        bind: Engine | Connection,
        task_id: UUID,
        execution_id: UUID | None,
        tool_call_id: UUID | None,
        policy: RecoveryPolicy,
    ) -> RecoveryResult:
        with Session(bind=bind, autoflush=False, expire_on_commit=False) as recovery_session:
            statement = (
                select(Task)
                .where(Task.id == task_id)
                .options(
                    selectinload(Task.execution).selectinload(Execution.tool_calls),
                    selectinload(Task.execution).selectinload(Execution.approval),
                )
                .with_for_update()
            )
            persisted_task = recovery_session.scalar(statement)
            if not TaskService._matches_recovery_policy(
                persisted_task,
                execution_id,
                tool_call_id,
                policy,
            ):
                recovery_session.rollback()
                return RecoveryResult.NOT_APPLIED

            if policy.target is RecoveryTarget.PRESERVE:
                recovery_session.rollback()
                return RecoveryResult.PRESERVED
            if persisted_task is None:
                raise RuntimeError("Matched recovery policy without a persisted task.")

            completed_at = utc_now()
            task_target = (
                TaskStatus.OUTCOME_UNCERTAIN
                if policy.target is RecoveryTarget.OUTCOME_UNCERTAIN
                else TaskStatus.FAILED
            )
            execution_target = (
                ExecutionStatus.OUTCOME_UNCERTAIN
                if policy.target is RecoveryTarget.OUTCOME_UNCERTAIN
                else ExecutionStatus.FAILED
            )
            tool_call_target = (
                ToolCallStatus.OUTCOME_UNCERTAIN
                if policy.target is RecoveryTarget.OUTCOME_UNCERTAIN
                else ToolCallStatus.FAILED
            )
            message = (
                UNCERTAIN_OUTCOME_MESSAGE
                if policy.target is RecoveryTarget.OUTCOME_UNCERTAIN
                else PERSISTED_FAILURE_MESSAGE
            )

            _transition_task(persisted_task, task_target)
            persisted_task.result = None
            persisted_task.error = message

            persisted_execution = persisted_task.execution
            if persisted_execution is not None:
                _transition_execution(persisted_execution, execution_target)
                persisted_execution.result = None
                persisted_execution.error = message
                persisted_execution.completed_at = completed_at

                persisted_tool_call = persisted_execution.tool_calls[0]
                _transition_tool_call(persisted_tool_call, tool_call_target)
                persisted_tool_call.result = None
                persisted_tool_call.error = message
                persisted_tool_call.completed_at = completed_at

            recovery_session.commit()
            if policy.target is RecoveryTarget.OUTCOME_UNCERTAIN:
                return RecoveryResult.OUTCOME_UNCERTAIN
            return RecoveryResult.FAILED

    @staticmethod
    def _matches_recovery_policy(
        task: Task | None,
        execution_id: UUID | None,
        tool_call_id: UUID | None,
        policy: RecoveryPolicy,
    ) -> bool:
        if task is None or task.status is not policy.expected_task_status:
            return False

        execution = task.execution
        if policy.expected_execution_status is None:
            return execution is None
        if (
            execution is None
            or execution_id is None
            or execution.id != execution_id
            or execution.status is not policy.expected_execution_status
            or len(execution.tool_calls) != 1
        ):
            return False

        tool_call = execution.tool_calls[0]
        if (
            tool_call_id is None
            or tool_call.id != tool_call_id
            or tool_call.status is not policy.expected_tool_call_status
        ):
            return False

        approval = execution.approval
        if policy.expected_approval_decision is None:
            return approval is None
        return approval is not None and approval.decision is policy.expected_approval_decision

    def reconcile_stranded_executions(
        self,
        *,
        before: datetime | None = None,
        limit: int = STRANDED_RECONCILIATION_LIMIT,
    ) -> int:
        """Finalize a bounded startup batch without ever re-executing a tool."""
        if limit < 1 or limit > STRANDED_RECONCILIATION_LIMIT:
            raise ValueError(
                f"Reconciliation limit must be between 1 and {STRANDED_RECONCILIATION_LIMIT}."
            )
        cutoff = before or utc_now()
        statement = (
            select(Task.id)
            .where(Task.status == TaskStatus.EXECUTING, Task.updated_at <= cutoff)
            .order_by(Task.updated_at, Task.id)
            .limit(limit)
        )
        try:
            task_ids = tuple(self._session.scalars(statement))
        except SQLAlchemyError as exc:
            self._rollback()
            raise TaskPersistenceError from exc

        reconciled = 0
        for task_id in task_ids:
            if self._reconcile_stranded_execution(task_id):
                reconciled += 1
        return reconciled

    def _reconcile_stranded_execution(self, task_id: UUID) -> bool:
        statement = (
            select(Task)
            .where(Task.id == task_id)
            .options(
                selectinload(Task.execution).selectinload(Execution.tool_calls),
                selectinload(Task.execution).selectinload(Execution.approval),
            )
            .with_for_update()
        )
        try:
            task = self._session.scalar(statement)
            if (
                task is None
                or task.status is not TaskStatus.EXECUTING
                or task.execution is None
                or task.execution.status is not ExecutionStatus.RUNNING
                or len(task.execution.tool_calls) != 1
                or task.execution.tool_calls[0].status is not ToolCallStatus.RUNNING
            ):
                self._session.rollback()
                return False

            execution = task.execution
            tool_call = execution.tool_calls[0]
            approved_write = (
                execution.approval is not None
                and execution.approval.decision is ApprovalDecision.APPROVED
            )
            completed_at = utc_now()
            if approved_write:
                task_target = TaskStatus.OUTCOME_UNCERTAIN
                execution_target = ExecutionStatus.OUTCOME_UNCERTAIN
                tool_call_target = ToolCallStatus.OUTCOME_UNCERTAIN
                message = UNCERTAIN_OUTCOME_MESSAGE
            else:
                task_target = TaskStatus.FAILED
                execution_target = ExecutionStatus.FAILED
                tool_call_target = ToolCallStatus.FAILED
                message = INTERRUPTED_EXECUTION_MESSAGE

            _transition_task(task, task_target)
            _transition_execution(execution, execution_target)
            _transition_tool_call(tool_call, tool_call_target)
            task.result = None
            task.error = message
            execution.result = None
            execution.error = message
            execution.completed_at = completed_at
            tool_call.result = None
            tool_call.error = message
            tool_call.completed_at = completed_at
            self._session.commit()
            return True
        except SQLAlchemyError as exc:
            self._rollback()
            logger.error(
                "stranded execution reconciliation failed task_id=%s error_type=%s",
                task_id,
                type(exc).__name__,
                extra={
                    "task_id": str(task_id),
                    "persistence_operation": "reconcile_stranded_execution",
                    "database_error_type": type(exc).__name__,
                },
            )
            raise TaskPersistenceError from exc

    def _rollback(self) -> bool:
        try:
            self._session.rollback()
        except SQLAlchemyError:
            return False
        return True

    @staticmethod
    def _log_execution_failure(
        error: AgentExecutionError,
        task: Task,
        execution: Execution | None,
        tool_call: ToolCall | None,
    ) -> None:
        logger.warning(
            "task execution failed task_id=%s execution_id=%s tool_call_id=%s stage=%s",
            task.id,
            execution.id if execution is not None else None,
            tool_call.id if tool_call is not None else None,
            error.stage,
            extra={
                "task_id": str(task.id),
                "execution_id": str(execution.id) if execution is not None else None,
                "tool_call_id": str(tool_call.id) if tool_call is not None else None,
                "failure_stage": error.stage,
            },
        )
