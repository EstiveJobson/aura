import json
from copy import deepcopy
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from app.core.constraints import (
    PLAN_ARGUMENT_MAX_PROPERTIES,
    PLAN_STEP_TITLE_MAX_LENGTH,
    PLAN_SUMMARY_MAX_LENGTH,
    PLANNER_NAME_MAX_LENGTH,
    TASK_INSTRUCTION_MAX_LENGTH,
    TOOL_NAME_MAX_LENGTH,
)
from app.providers import AIProvider, ProviderMessage, StructuredGenerationRequest
from app.tools.base import ToolDefinition


class PlannedStep(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    sequence: int = Field(ge=1, le=1)
    title: str = Field(min_length=1, max_length=PLAN_STEP_TITLE_MAX_LENGTH)
    tool_name: str = Field(min_length=1, max_length=TOOL_NAME_MAX_LENGTH)
    arguments: dict[str, Any] = Field(max_length=PLAN_ARGUMENT_MAX_PROPERTIES)


class GeneratedPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    summary: str = Field(min_length=1, max_length=PLAN_SUMMARY_MAX_LENGTH)
    steps: tuple[PlannedStep] = Field(min_length=1, max_length=1)


class ExecutionPlan(GeneratedPlan):
    planner: str = Field(min_length=1, max_length=PLANNER_NAME_MAX_LENGTH)


class Planner(Protocol):
    def create_plan(self, instruction: str) -> GeneratedPlan: ...


class MockPlanner:
    """The deterministic planner used for tests and local development."""

    def create_plan(self, instruction: str) -> GeneratedPlan:
        prefix = 'Inspect the configured workspace for "'
        suffix = '".'
        excerpt = instruction[: PLAN_SUMMARY_MAX_LENGTH - len(prefix) - len(suffix)]
        return GeneratedPlan(
            summary=f"{prefix}{excerpt}{suffix}",
            steps=(
                PlannedStep(
                    sequence=1,
                    title="List top-level workspace entries",
                    tool_name="workspace_list",
                    arguments={},
                ),
            ),
        )


class LLMPlanner:
    """Create one locally validated plan from a provider's structured response."""

    def __init__(
        self,
        provider: AIProvider,
        tool_definitions: tuple[ToolDefinition, ...],
    ) -> None:
        if not tool_definitions:
            raise ValueError("The planner requires at least one registered tool.")
        self._provider = provider
        self._tool_definitions = tuple(
            ToolDefinition.model_validate(definition) for definition in tool_definitions
        )
        self._output_schema = self._build_output_schema(self._tool_definitions)

    def create_plan(self, instruction: str) -> GeneratedPlan:
        normalized_instruction = instruction.strip()
        if not normalized_instruction or len(normalized_instruction) > TASK_INSTRUCTION_MAX_LENGTH:
            raise ValueError("The planner instruction is invalid.")

        tool_catalog = [definition.model_dump(mode="json") for definition in self._tool_definitions]
        request = StructuredGenerationRequest(
            messages=(
                ProviderMessage(
                    role="system",
                    content=(
                        "Create one bounded AURA execution-plan step for the user's request. "
                        "Select only a registered tool and arguments allowed by its schema. "
                        "Do not invent tools, paths, extra steps, or execution instructions. "
                        "Do not reveal chain-of-thought. Registered tool definitions: "
                        f"{json.dumps(tool_catalog, separators=(',', ':'))}"
                    ),
                ),
                ProviderMessage(role="user", content=normalized_instruction),
            ),
            schema_name="aura_single_step_plan",
            output_schema=self._output_schema,
        )
        payload = self._provider.generate(request)
        return GeneratedPlan.model_validate(payload)

    @staticmethod
    def _build_output_schema(tools: tuple[ToolDefinition, ...]) -> dict[str, Any]:
        step_variants = [
            {
                "type": "object",
                "properties": {
                    "sequence": {"type": "integer", "enum": [1]},
                    "title": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": PLAN_STEP_TITLE_MAX_LENGTH,
                    },
                    "tool_name": {"type": "string", "enum": [tool.name]},
                    "arguments": deepcopy(tool.parameters),
                },
                "required": ["sequence", "title", "tool_name", "arguments"],
                "additionalProperties": False,
            }
            for tool in tools
        ]
        return {
            "type": "object",
            "properties": {
                "summary": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": PLAN_SUMMARY_MAX_LENGTH,
                },
                "steps": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 1,
                    "items": {"anyOf": step_variants},
                },
            },
            "required": ["summary", "steps"],
            "additionalProperties": False,
        }
