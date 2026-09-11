from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field


class PlannedStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sequence: int = Field(ge=1)
    title: str = Field(min_length=1)
    tool_name: str = Field(min_length=1)
    arguments: dict[str, Any]


class ExecutionPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    planner: str = Field(min_length=1)
    summary: str = Field(min_length=1)
    steps: tuple[PlannedStep]


class Planner(Protocol):
    def create_plan(self, instruction: str) -> ExecutionPlan: ...


class MockPlanner:
    """The deterministic Phase 1 planner; it always selects the sole registered tool."""

    def create_plan(self, instruction: str) -> ExecutionPlan:
        return ExecutionPlan(
            planner="mock-planner-v1",
            summary=f'Inspect the configured workspace for "{instruction}".',
            steps=(
                PlannedStep(
                    sequence=1,
                    title="List top-level workspace entries",
                    tool_name="workspace_list",
                    arguments={},
                ),
            ),
        )
