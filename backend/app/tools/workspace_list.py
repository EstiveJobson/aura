from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from app.tools.base import PermissionLevel, ToolDefinition
from app.tools.workspace_paths import WorkspaceBoundary, serialized_item_size

MAX_LIST_VISITED_ENTRIES = 2_048
MAX_LIST_VISITED_DIRECTORIES = 1
MAX_LIST_DEPTH = 0
MAX_LIST_RESULTS = 1_000
MAX_LIST_RETURN_BYTES = 64 * 1024
MAX_LIST_TRAVERSAL_WORK = MAX_LIST_VISITED_ENTRIES + MAX_LIST_VISITED_DIRECTORIES


class WorkspaceListArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")


class WorkspaceListTool:
    """List immediate entries inside one preconfigured workspace without mutating it."""

    def __init__(self, workspace_root: Path) -> None:
        self._boundary = WorkspaceBoundary(workspace_root)

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
        self.validate_arguments(arguments)
        batch = self._boundary.directory_batch(".", MAX_LIST_VISITED_ENTRIES)
        entries: list[dict[str, str]] = []
        returned_bytes = 0
        truncated = batch.truncated
        for name in batch.names:
            kind = self._boundary.entry_kind(name)
            if kind is None:
                continue
            item = {"name": name, "kind": kind}
            item_size = serialized_item_size(item)
            if (
                len(entries) >= MAX_LIST_RESULTS
                or returned_bytes + item_size > MAX_LIST_RETURN_BYTES
            ):
                truncated = True
                break
            entries.append(item)
            returned_bytes += item_size

        count = len(entries)
        noun = "entry" if count == 1 else "entries"
        qualifier = " (truncated)" if truncated else ""
        return {
            "workspace": self._boundary.workspace_name,
            "entries": entries,
            "entry_count": count,
            "visited_entries": batch.visited_entries,
            "visited_directories": MAX_LIST_VISITED_DIRECTORIES,
            "max_depth": MAX_LIST_DEPTH,
            "returned_bytes": returned_bytes,
            "traversal_work": batch.visited_entries + MAX_LIST_VISITED_DIRECTORIES,
            "truncated": truncated,
            "summary": (f"Found {count} top-level {noun} in the configured workspace{qualifier}."),
        }

    def validate_arguments(self, arguments: dict[str, Any]) -> None:
        WorkspaceListArguments.model_validate(arguments)
