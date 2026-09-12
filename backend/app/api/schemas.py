from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.constraints import TASK_INSTRUCTION_MAX_LENGTH
from app.models import ApprovalDecision, ExecutionStatus, TaskStatus, ToolCallStatus


class TaskCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    instruction: str = Field(min_length=1, max_length=TASK_INSTRUCTION_MAX_LENGTH)

    @field_validator("instruction")
    @classmethod
    def normalize_instruction(cls, value: str) -> str:
        instruction = value.strip()
        if not instruction:
            raise ValueError("Instruction must not be blank.")
        return instruction


class PlannedStepResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    sequence: int
    title: str
    tool_name: str
    arguments: dict[str, Any]


class PlanResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    planner: str
    summary: str
    steps: list[PlannedStepResponse]
    created_at: datetime


class ToolCallResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    tool_name: str
    arguments: dict[str, Any]
    status: ToolCallStatus
    result: dict[str, Any] | None
    error: str | None
    started_at: datetime
    completed_at: datetime | None


class ApprovalResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    decision: ApprovalDecision
    requested_at: datetime
    decided_at: datetime | None


class ExecutionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    status: ExecutionStatus
    result: dict[str, Any] | None
    error: str | None
    started_at: datetime
    completed_at: datetime | None
    tool_calls: list[ToolCallResponse]
    approval: ApprovalResponse | None


class TaskResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    instruction: str
    status: TaskStatus
    result: str | None
    error: str | None
    created_at: datetime
    updated_at: datetime
    plan: PlanResponse | None
    execution: ExecutionResponse | None


class ErrorResponse(BaseModel):
    code: str
    message: str
