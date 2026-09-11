from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from app.tools.base import PermissionLevel, ToolDefinition


class WorkspaceListArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")


class WorkspaceListTool:
    """List immediate entries inside one preconfigured workspace without mutating it."""

    def __init__(self, workspace_root: Path) -> None:
        self._workspace_root = workspace_root.resolve(strict=True)

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="workspace_list",
            description=(
                "List the names and kinds of top-level entries in the configured workspace."
            ),
            permission=PermissionLevel.READ,
            parameters=WorkspaceListArguments.model_json_schema(),
        )

    def execute(self, arguments: dict[str, Any]) -> dict[str, Any]:
        WorkspaceListArguments.model_validate(arguments)
        entries = [
            {"name": entry.name, "kind": "directory" if entry.is_dir() else "file"}
            for entry in sorted(
                self._workspace_root.iterdir(), key=lambda path: path.name.casefold()
            )
        ]
        count = len(entries)
        noun = "entry" if count == 1 else "entries"
        return {
            "workspace": self._workspace_root.name,
            "entries": entries,
            "entry_count": count,
            "summary": f"Found {count} top-level {noun} in the configured workspace.",
        }
