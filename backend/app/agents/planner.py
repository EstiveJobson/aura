from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from app.core.constraints import PLANNER_NAME_MAX_LENGTH, TOOL_NAME_MAX_LENGTH


class PlannedStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sequence: int = Field(ge=1)
    title: str = Field(min_length=1)
    tool_name: str = Field(min_length=1, max_length=TOOL_NAME_MAX_LENGTH)
    arguments: dict[str, Any]


class GeneratedPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str = Field(min_length=1)
    steps: tuple[PlannedStep] = Field(min_length=1, max_length=1)


class ExecutionPlan(GeneratedPlan):
    planner: str = Field(min_length=1, max_length=PLANNER_NAME_MAX_LENGTH)


class Planner(Protocol):
    def create_plan(self, instruction: str) -> GeneratedPlan: ...


class MockPlanner:
    """The deterministic Phase 1 planner; it always selects the sole registered tool."""

    def create_plan(self, instruction: str) -> GeneratedPlan:
        return GeneratedPlan(
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
