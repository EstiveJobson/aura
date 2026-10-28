from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from app.core.constraints import TOOL_NAME_MAX_LENGTH


class PermissionLevel(StrEnum):
    READ = "read"
    WRITE = "write"


class MutationOutcomeUnknown(RuntimeError):
    """A mutating primitive was accepted but its outcome cannot be established."""


class ToolDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=TOOL_NAME_MAX_LENGTH)
    description: str
    permission: PermissionLevel
    parameters: dict[str, Any]


class Tool(Protocol):
    @property
    def definition(self) -> ToolDefinition: ...

    def validate_arguments(self, arguments: dict[str, Any]) -> None: ...

    def execute(self, arguments: dict[str, Any]) -> dict[str, Any]: ...


@runtime_checkable
class ApprovalBoundTool(Protocol):
    """Internal contract for WRITE tools with application-owned preconditions."""

    def capture_approval_context(self, arguments: dict[str, Any]) -> dict[str, Any]: ...

    def execute(
        self,
        arguments: dict[str, Any],
        *,
        approval_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]: ...
