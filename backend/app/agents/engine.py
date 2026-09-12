from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.agents.planner import ExecutionPlan, GeneratedPlan, Planner
from app.core.constraints import PLANNER_NAME_MAX_LENGTH
from app.tools.registry import ToolError, ToolExecutor


class AgentExecutionError(RuntimeError):
    """A safe, user-visible failure raised by the deterministic engine."""

    def __init__(
        self,
        message: str,
        *,
        stage: Literal["planner", "plan_validation", "tool", "execution"],
    ) -> None:
        super().__init__(message)
        self.stage = stage


class PlannerIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=PLANNER_NAME_MAX_LENGTH)


@dataclass(frozen=True)
class AgentExecutionResult:
    tool_name: str
    output: dict[str, Any]
    final_result: str


class AgentEngine:
    def __init__(
        self,
        planner: Planner,
        tool_executor: ToolExecutor,
        *,
        planner_name: str,
    ) -> None:
        self._planner = planner
        self._tool_executor = tool_executor
        self._planner_name = PlannerIdentity(name=planner_name).name

    def create_plan(self, instruction: str) -> ExecutionPlan:
        try:
            generated = self._planner.create_plan(instruction)
        except Exception as exc:
            raise AgentExecutionError(
                "The planner could not create a plan.",
                stage="planner",
            ) from exc

        try:
            validated = GeneratedPlan.model_validate(generated)
            step = validated.steps[0]
            self._tool_executor.validate_call(step.tool_name, step.arguments)
        except (ValidationError, ToolError) as exc:
            raise AgentExecutionError(
                "The generated plan was rejected.",
                stage="plan_validation",
            ) from exc

        return ExecutionPlan(
            planner=self._planner_name,
            summary=validated.summary,
            steps=validated.steps,
        )

    def requires_approval(self, plan: ExecutionPlan) -> bool:
        step = plan.steps[0]
        try:
            return self._tool_executor.requires_approval(step.tool_name)
        except ToolError as exc:
            raise AgentExecutionError(
                "The generated plan was rejected.",
                stage="plan_validation",
            ) from exc

    def capture_approval_context(self, plan: ExecutionPlan) -> dict[str, Any]:
        step = plan.steps[0]
        try:
            return self._tool_executor.capture_approval_context(
                step.tool_name,
                step.arguments,
            )
        except ToolError as exc:
            raise AgentExecutionError(str(exc), stage="tool") from exc

    def execute(
        self,
        plan: ExecutionPlan,
        *,
        approved: bool = False,
        approval_context: dict[str, Any] | None = None,
    ) -> AgentExecutionResult:
        step = plan.steps[0]
        try:
            output = self._tool_executor.execute(
                step.tool_name,
                step.arguments,
                approved=approved,
                approval_context=approval_context,
            )
        except ToolError as exc:
            raise AgentExecutionError(str(exc), stage="tool") from exc

        summary = output.get("summary")
        if not isinstance(summary, str):
            raise AgentExecutionError(
                "The tool returned an invalid result.",
                stage="execution",
            )
        return AgentExecutionResult(
            tool_name=step.tool_name,
            output=output,
            final_result=summary,
        )
