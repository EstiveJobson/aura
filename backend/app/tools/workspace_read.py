from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.tools.base import PermissionLevel, ToolDefinition
from app.tools.workspace_paths import WorkspaceBoundary, validate_relative_path

MAX_READ_BYTES = 256 * 1024


class WorkspaceReadArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(
        min_length=1,
        max_length=500,
        description="Workspace-relative path to one UTF-8 text file.",
    )

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return validate_relative_path(value, allow_root=False)


class WorkspaceReadTool:
    """Read one bounded UTF-8 text file without leaving the workspace."""

    def __init__(self, workspace_root: Path) -> None:
        self._boundary = WorkspaceBoundary(workspace_root)

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="workspace_read",
            description=(
                "Read one UTF-8 text file using a workspace-relative path. "
                "Binary, symlinked, unavailable, and oversized files are rejected."
            ),
            permission=PermissionLevel.READ,
            parameters=WorkspaceReadArguments.model_json_schema(),
        )

    def validate_arguments(self, arguments: dict[str, Any]) -> None:
        WorkspaceReadArguments.model_validate(arguments)

    def execute(self, arguments: dict[str, Any]) -> dict[str, Any]:
        validated = WorkspaceReadArguments.model_validate(arguments)
        try:
            payload = self._boundary.read_regular_file(validated.path, MAX_READ_BYTES)
        except OSError as exc:
            raise ValueError("The requested file is unavailable or unreadable.") from exc

        if b"\x00" in payload:
            raise ValueError("The requested file is binary or unsupported.")
        try:
            content = payload.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ValueError("The requested file is binary or unsupported.") from exc

        relative_path = validated.path
        return {
            "path": relative_path,
            "size_bytes": len(payload),
            "content": content,
            "summary": f'Read {len(payload)} bytes from "{relative_path}".',
        }
