from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.tools.base import PermissionLevel, ToolDefinition
from app.tools.workspace_access import ReadOnlyWorkspaceAccess, WorkspaceWriteAccess
from app.tools.workspace_paths import WorkspaceBoundary, validate_relative_path


class WorkspaceMoveArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: str = Field(
        min_length=1,
        max_length=500,
        description="Workspace-relative path of the existing file to move or rename.",
    )
    destination: str = Field(
        min_length=1,
        max_length=500,
        description="Workspace-relative destination path in an existing directory.",
    )

    @field_validator("source", "destination")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return validate_relative_path(value, allow_root=False)

    @model_validator(mode="after")
    def reject_same_path(self) -> "WorkspaceMoveArguments":
        if self.source.casefold() == self.destination.casefold():
            raise ValueError("Source and destination must be different.")
        return self


class WorkspaceMoveTool:
    """Move one regular file within the workspace without overwriting."""

    def __init__(
        self,
        workspace_root: Path,
        write_access: WorkspaceWriteAccess | None = None,
    ) -> None:
        self._boundary = WorkspaceBoundary(workspace_root)
        self._write_access = write_access or ReadOnlyWorkspaceAccess()

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="workspace_move",
            description=(
                "Move or rename one regular file between workspace-relative paths. "
                "The destination directory must exist and an existing destination is never "
                "overwritten."
            ),
            permission=PermissionLevel.WRITE,
            parameters=WorkspaceMoveArguments.model_json_schema(),
        )

    def validate_arguments(self, arguments: dict[str, Any]) -> None:
        WorkspaceMoveArguments.model_validate(arguments)

    def capture_approval_context(self, arguments: dict[str, Any]) -> dict[str, Any]:
        validated = WorkspaceMoveArguments.model_validate(arguments)
        self._write_access.assert_write_allowed()
        return self._boundary.approval_context(validated.source)

    def execute(
        self,
        arguments: dict[str, Any],
        *,
        approval_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        validated = WorkspaceMoveArguments.model_validate(arguments)
        if approval_context is None:
            raise ValueError("The persisted filesystem approval is required.")
        self._write_access.assert_write_allowed()
        self._boundary.move_no_replace(
            validated.source,
            validated.destination,
            approval_context,
        )

        source_path = validated.source
        destination_path = validated.destination
        return {
            "source": source_path,
            "destination": destination_path,
            "summary": f'Moved "{source_path}" to "{destination_path}".',
        }
