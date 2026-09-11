from dataclasses import dataclass
from typing import Any

from app.agents.planner import ExecutionPlan, Planner
from app.tools.registry import ToolError, ToolExecutor


class AgentExecutionError(RuntimeError):
    """A safe, user-visible failure raised by the deterministic engine."""


@dataclass(frozen=True)
class AgentExecutionResult:
    tool_name: str
    output: dict[str, Any]
    final_result: str


class AgentEngine:
    def __init__(self, planner: Planner, tool_executor: ToolExecutor) -> None:
        self._planner = planner
        self._tool_executor = tool_executor

    def create_plan(self, instruction: str) -> ExecutionPlan:
        try:
            return self._planner.create_plan(instruction)
        except Exception as exc:
            raise AgentExecutionError("The deterministic planner could not create a plan.") from exc

    def execute(self, plan: ExecutionPlan) -> AgentExecutionResult:
        step = plan.steps[0]
        try:
            output = self._tool_executor.execute(step.tool_name, step.arguments)
        except ToolError as exc:
            raise AgentExecutionError(str(exc)) from exc

        summary = output.get("summary")
        if not isinstance(summary, str):
            raise AgentExecutionError("The tool returned an invalid result.")
        return AgentExecutionResult(
            tool_name=step.tool_name,
            output=output,
            final_result=summary,
        )
