from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.tools.base import PermissionLevel, ToolDefinition
from app.tools.workspace_paths import WorkspaceBoundary, validate_relative_path

MAX_READ_BYTES = 256 * 1024


class WorkspaceReadArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

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
            path = self._boundary.resolve_existing(validated.path)
            if not path.is_file():
                raise OSError
            with path.open("rb") as stream:
                payload = stream.read(MAX_READ_BYTES + 1)
        except OSError as exc:
            raise ValueError("The requested file is unavailable or unreadable.") from exc

        if len(payload) > MAX_READ_BYTES:
            raise ValueError("The requested file exceeds the read limit.")
        if b"\x00" in payload:
            raise ValueError("The requested file is binary or unsupported.")
        try:
            content = payload.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ValueError("The requested file is binary or unsupported.") from exc

        relative_path = path.relative_to(self._boundary.root).as_posix()
        return {
            "path": relative_path,
            "size_bytes": len(payload),
            "content": content,
            "summary": f'Read {len(payload)} bytes from "{relative_path}".',
        }
