import logging
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
TASK_NOT_FOUND_MESSAGE = "Task was not found."
TASK_STATE_CONFLICT_MESSAGE = "Task is not waiting for approval."


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


class TaskService:
    def __init__(self, session: Session, agent_engine: AgentEngine) -> None:
        self._session = session
        self._agent_engine = agent_engine

    def create_and_execute(self, instruction: str) -> Task:
        task = Task(id=uuid4(), instruction=instruction, status=TaskStatus.PENDING)
        self._session.add(task)
        self._commit_or_raise("create_task", task)

        plan_record: Plan | None = None
        execution: Execution | None = None
        tool_call: ToolCall | None = None
        try:
            task.status = TaskStatus.PLANNING
            self._commit_or_raise("start_planning", task)

            plan = self._agent_engine.create_plan(instruction)
            plan_record = Plan(
                task=task,
                planner=plan.planner,
                summary=plan.summary,
                steps=[step.model_dump(mode="json") for step in plan.steps],
            )
            requires_approval = self._agent_engine.requires_approval(plan)
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
            self._flush_or_raise("prepare_execution", task, execution)

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
                task.status = TaskStatus.WAITING_FOR_APPROVAL
                self._session.add(
                    Approval(
                        execution=execution,
                        decision=ApprovalDecision.PENDING,
                    )
                )
                self._commit_or_raise("await_approval", task, execution, tool_call)
            else:
                task.status = TaskStatus.EXECUTING
                self._commit_or_raise("start_execution", task, execution, tool_call)

                outcome = self._agent_engine.execute(plan)
                self._complete_execution(
                    task,
                    execution,
                    tool_call,
                    outcome.output,
                    outcome.final_result,
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
        approval.decision = ApprovalDecision.APPROVED
        approval.decided_at = decided_at
        task.status = TaskStatus.EXECUTING
        execution.status = ExecutionStatus.RUNNING
        tool_call.status = ToolCallStatus.RUNNING
        self._commit_or_raise("approve_execution", task, execution, tool_call)

        try:
            outcome = self._agent_engine.execute(plan, approved=True)
            self._complete_execution(
                task,
                execution,
                tool_call,
                outcome.output,
                outcome.final_result,
            )
        except AgentExecutionError as exc:
            self._log_execution_failure(exc, task, execution, tool_call)
            self._mark_execution_failed(task, execution, tool_call, str(exc))
        return self._reload_created_task(task, execution, tool_call)

    def reject(self, task_id: UUID) -> Task:
        task = self._get_for_decision(task_id)
        execution, tool_call, approval = self._require_waiting_for_approval(task)
        completed_at = utc_now()
        approval.decision = ApprovalDecision.REJECTED
        approval.decided_at = completed_at
        task.status = TaskStatus.REJECTED
        task.result = None
        task.error = None
        execution.status = ExecutionStatus.REJECTED
        execution.result = None
        execution.error = None
        execution.completed_at = completed_at
        tool_call.status = ToolCallStatus.REJECTED
        tool_call.result = None
        tool_call.error = None
        tool_call.completed_at = completed_at
        self._commit_or_raise("reject_execution", task, execution, tool_call)
        return self._reload_created_task(task, execution, tool_call)

    def _reload_created_task(
        self,
        task: Task,
        execution: Execution | None,
        tool_call: ToolCall | None,
    ) -> Task:
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
    ) -> None:
        completed_at = utc_now()
        tool_call.status = ToolCallStatus.SUCCEEDED
        tool_call.result = output
        tool_call.error = None
        tool_call.completed_at = completed_at
        execution.status = ExecutionStatus.SUCCEEDED
        execution.result = output
        execution.error = None
        execution.completed_at = completed_at
        task.status = TaskStatus.SUCCEEDED
        task.result = final_result
        task.error = None
        self._commit_or_raise("complete_execution", task, execution, tool_call)

    def _mark_execution_failed(
        self,
        task: Task,
        execution: Execution | None,
        tool_call: ToolCall | None,
        error: str,
    ) -> None:
        completed_at = utc_now()
        if tool_call is not None:
            tool_call.status = ToolCallStatus.FAILED
            tool_call.result = None
            tool_call.error = error
            tool_call.completed_at = completed_at
        if execution is not None:
            execution.status = ExecutionStatus.FAILED
            execution.result = None
            execution.error = error
            execution.completed_at = completed_at
        task.status = TaskStatus.FAILED
        task.result = None
        task.error = error
        self._commit_or_raise("record_execution_failure", task, execution, tool_call)

    def _commit_or_raise(
        self,
        operation: str,
        task: Task,
        execution: Execution | None = None,
        tool_call: ToolCall | None = None,
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
            )

    def _flush_or_raise(
        self,
        operation: str,
        task: Task,
        execution: Execution | None = None,
        tool_call: ToolCall | None = None,
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
            )

    def _handle_write_failure(
        self,
        operation: str,
        error: SQLAlchemyError,
        task_id: UUID,
        execution_id: UUID | None,
        tool_call_id: UUID | None,
    ) -> None:
        bind = self._session.get_bind()
        rollback_succeeded = self._rollback()
        failure_state_persisted = False
        recovery_error_type: str | None = None

        if rollback_succeeded:
            try:
                failure_state_persisted = self._recover_failed_lifecycle(
                    bind,
                    task_id,
                    execution_id,
                    tool_call_id,
                )
            except SQLAlchemyError as recovery_error:
                recovery_error_type = type(recovery_error).__name__

        logger.error(
            (
                "task persistence write failed task_id=%s execution_id=%s "
                "tool_call_id=%s operation=%s error_type=%s rollback_succeeded=%s "
                "failure_state_persisted=%s recovery_error_type=%s"
            ),
            task_id,
            execution_id,
            tool_call_id,
            operation,
            type(error).__name__,
            rollback_succeeded,
            failure_state_persisted,
            recovery_error_type,
            extra={
                "task_id": str(task_id),
                "execution_id": str(execution_id) if execution_id is not None else None,
                "tool_call_id": str(tool_call_id) if tool_call_id is not None else None,
                "persistence_operation": operation,
                "database_error_type": type(error).__name__,
                "rollback_succeeded": rollback_succeeded,
                "failure_state_persisted": failure_state_persisted,
                "recovery_error_type": recovery_error_type,
            },
        )
        raise TaskPersistenceError from error

    @staticmethod
    def _recover_failed_lifecycle(
        bind: Engine | Connection,
        task_id: UUID,
        execution_id: UUID | None,
        tool_call_id: UUID | None,
    ) -> bool:
        with Session(bind=bind, autoflush=False, expire_on_commit=False) as recovery_session:
            persisted_task = recovery_session.get(Task, task_id)
            if persisted_task is None:
                return False

            completed_at = utc_now()
            persisted_task.status = TaskStatus.FAILED
            persisted_task.result = None
            persisted_task.error = PERSISTED_FAILURE_MESSAGE

            if execution_id is not None:
                persisted_execution = recovery_session.get(Execution, execution_id)
                if persisted_execution is not None:
                    persisted_execution.status = ExecutionStatus.FAILED
                    persisted_execution.result = None
                    persisted_execution.error = PERSISTED_FAILURE_MESSAGE
                    persisted_execution.completed_at = completed_at

            if tool_call_id is not None:
                persisted_tool_call = recovery_session.get(ToolCall, tool_call_id)
                if persisted_tool_call is not None:
                    persisted_tool_call.status = ToolCallStatus.FAILED
                    persisted_tool_call.result = None
                    persisted_tool_call.error = PERSISTED_FAILURE_MESSAGE
                    persisted_tool_call.completed_at = completed_at

            recovery_session.commit()
            return True

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
