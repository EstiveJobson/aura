from enum import StrEnum
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict


class PermissionLevel(StrEnum):
    READ = "read"


class ToolDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    description: str
    permission: PermissionLevel
    parameters: dict[str, Any]


class Tool(Protocol):
    @property
    def definition(self) -> ToolDefinition: ...

    def execute(self, arguments: dict[str, Any]) -> dict[str, Any]: ...
