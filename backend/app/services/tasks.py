from uuid import UUID

from sqlalchemy import select
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


class TaskService:
    def __init__(self, session: Session, agent_engine: AgentEngine) -> None:
        self._session = session
        self._agent_engine = agent_engine

    def create_and_execute(self, instruction: str) -> Task:
        task = Task(instruction=instruction, status=TaskStatus.PENDING)
        self._session.add(task)
        self._session.commit()

        plan_record: Plan | None = None
        execution: Execution | None = None
        tool_call: ToolCall | None = None
        try:
            task.status = TaskStatus.PLANNING
            self._session.commit()

            plan = self._agent_engine.create_plan(instruction)
            plan_record = Plan(
                task=task,
                planner=plan.planner,
                summary=plan.summary,
                steps=[step.model_dump(mode="json") for step in plan.steps],
            )
            execution = Execution(task=task, status=ExecutionStatus.RUNNING)
            self._session.add_all((plan_record, execution))
            self._session.flush()

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
            self._session.commit()

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
            self._session.commit()
        except AgentExecutionError as exc:
            completed_at = utc_now()
            if tool_call is not None:
                tool_call.status = ToolCallStatus.FAILED
                tool_call.error = str(exc)
                tool_call.completed_at = completed_at
            if execution is not None:
                execution.status = ExecutionStatus.FAILED
                execution.error = str(exc)
                execution.completed_at = completed_at
            task.status = TaskStatus.FAILED
            task.error = str(exc)
            self._session.commit()

        persisted_task = self.get(task.id)
        if persisted_task is None:
            raise RuntimeError("The persisted task could not be reloaded.")
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
        return self._session.scalar(statement)
