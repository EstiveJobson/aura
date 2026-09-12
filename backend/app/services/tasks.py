import logging
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, selectinload

from app.agents.engine import AgentEngine, AgentExecutionError
from app.models import (
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


class TaskPersistenceError(RuntimeError):
    """A predictable, sanitized infrastructure failure for the API boundary."""

    code = "task_persistence_failed"

    def __init__(self) -> None:
        super().__init__(PERSISTENCE_FAILURE_MESSAGE)
        self.message = PERSISTENCE_FAILURE_MESSAGE


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
            execution = Execution(task=task, status=ExecutionStatus.RUNNING)
            self._session.add_all((plan_record, execution))
            self._flush_or_raise("prepare_execution", task, execution)

            step = plan.steps[0]
            tool_call = ToolCall(
                execution=execution,
                plan_id=plan_record.id,
                tool_name=step.tool_name,
                arguments=step.arguments,
                status=ToolCallStatus.RUNNING,
            )
            task.status = TaskStatus.EXECUTING
            self._session.add(tool_call)
            self._commit_or_raise("start_execution", task, execution, tool_call)

            outcome = self._agent_engine.execute(plan)
            completed_at = utc_now()
            tool_call.status = ToolCallStatus.SUCCEEDED
            tool_call.result = outcome.output
            tool_call.completed_at = completed_at
            execution.status = ExecutionStatus.SUCCEEDED
            execution.result = outcome.output
            execution.completed_at = completed_at
            task.status = TaskStatus.SUCCEEDED
            task.result = outcome.final_result
            self._commit_or_raise("complete_execution", task, execution, tool_call)
        except AgentExecutionError as exc:
            self._log_execution_failure(exc, task, execution, tool_call)
            self._mark_execution_failed(task, execution, tool_call, str(exc))

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
            tool_call.error = error
            tool_call.completed_at = completed_at
        if execution is not None:
            execution.status = ExecutionStatus.FAILED
            execution.error = error
            execution.completed_at = completed_at
        task.status = TaskStatus.FAILED
        task.error = error
        self._commit_or_raise("record_execution_failure", task, execution, tool_call)

    def _commit_or_raise(
        self,
        operation: str,
        task: Task,
        execution: Execution | None = None,
        tool_call: ToolCall | None = None,
    ) -> None:
        try:
            self._session.commit()
        except SQLAlchemyError as exc:
            self._handle_write_failure(operation, exc, task, execution, tool_call)

    def _flush_or_raise(
        self,
        operation: str,
        task: Task,
        execution: Execution | None = None,
        tool_call: ToolCall | None = None,
    ) -> None:
        try:
            self._session.flush()
        except SQLAlchemyError as exc:
            self._handle_write_failure(operation, exc, task, execution, tool_call)

    def _handle_write_failure(
        self,
        operation: str,
        error: SQLAlchemyError,
        task: Task,
        execution: Execution | None,
        tool_call: ToolCall | None,
    ) -> None:
        task_id = task.id
        execution_id = execution.id if execution is not None else None
        tool_call_id = tool_call.id if tool_call is not None else None
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
