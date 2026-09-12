import os
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.tools.base import PermissionLevel, ToolDefinition
from app.tools.workspace_paths import WorkspaceBoundary, validate_relative_path


class WorkspaceMoveArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

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

    def __init__(self, workspace_root: Path) -> None:
        self._boundary = WorkspaceBoundary(workspace_root)

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

    def execute(self, arguments: dict[str, Any]) -> dict[str, Any]:
        validated = WorkspaceMoveArguments.model_validate(arguments)
        source = self._boundary.resolve_existing(validated.source)
        destination = self._boundary.resolve_destination(validated.destination)

        if not source.is_file():
            raise ValueError("Only regular files can be moved.")
        if destination.exists() or destination.is_symlink():
            raise FileExistsError("The destination already exists.")
        if source == destination:
            raise ValueError("Source and destination must be different.")

        destination_created = False
        try:
            os.link(source, destination, follow_symlinks=False)
            destination_created = True
            source.unlink()
        except Exception:
            if destination_created and destination.exists():
                try:
                    destination.unlink()
                except OSError:
                    pass
            raise

        source_path = validated.source
        destination_path = validated.destination
        return {
            "source": source_path,
            "destination": destination_path,
            "summary": f'Moved "{source_path}" to "{destination_path}".',
        }
