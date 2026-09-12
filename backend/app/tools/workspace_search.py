import os
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.tools.base import PermissionLevel, ToolDefinition
from app.tools.workspace_paths import WorkspaceBoundary, validate_relative_path

MAX_SEARCH_RESULTS = 100
MAX_SCANNED_FILES = 2_000
MAX_SEARCH_FILE_BYTES = 128 * 1024
MAX_EXCERPT_CHARACTERS = 240


class WorkspaceSearchArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    query: str = Field(
        min_length=1,
        max_length=200,
        description="Literal case-insensitive text to find in file names or UTF-8 file contents.",
    )
    path: str = Field(
        min_length=1,
        max_length=500,
        description="Workspace-relative directory to search; use '.' for the workspace root.",
    )
    max_results: int = Field(ge=1, le=MAX_SEARCH_RESULTS)

    @field_validator("query")
    @classmethod
    def validate_query(cls, value: str) -> str:
        if "\x00" in value:
            raise ValueError("The search query is invalid.")
        return value

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return validate_relative_path(value, allow_root=True)


class WorkspaceSearchTool:
    """Perform a bounded literal search without following symbolic links."""

    def __init__(self, workspace_root: Path) -> None:
        self._boundary = WorkspaceBoundary(workspace_root)

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="workspace_search",
            description=(
                "Search file names and UTF-8 text content below a workspace-relative directory. "
                "The query, result count, scanned files, file sizes, and excerpts are bounded."
            ),
            permission=PermissionLevel.READ,
            parameters=WorkspaceSearchArguments.model_json_schema(),
        )

    def validate_arguments(self, arguments: dict[str, Any]) -> None:
        WorkspaceSearchArguments.model_validate(arguments)

    def execute(self, arguments: dict[str, Any]) -> dict[str, Any]:
        validated = WorkspaceSearchArguments.model_validate(arguments)
        search_root = self._boundary.resolve_existing(validated.path, allow_root=True)
        if not search_root.is_dir():
            raise ValueError("The requested search path is not a directory.")

        needle = validated.query.casefold()
        results: list[dict[str, Any]] = []
        scanned_files = 0
        truncated = False

        for current_root, directory_names, file_names in os.walk(search_root, followlinks=False):
            current = Path(current_root)
            directory_names[:] = sorted(
                (
                    name
                    for name in directory_names
                    if self._boundary.is_safe_existing(current / name)
                ),
                key=str.casefold,
            )
            for file_name in sorted(file_names, key=str.casefold):
                path = current / file_name
                if not self._boundary.is_safe_existing(path) or not path.is_file():
                    continue
                if scanned_files >= MAX_SCANNED_FILES:
                    truncated = True
                    break
                scanned_files += 1
                relative_path = path.relative_to(self._boundary.root).as_posix()

                if needle in relative_path.casefold():
                    results.append(
                        {
                            "path": relative_path,
                            "match_type": "name",
                            "line": None,
                            "excerpt": relative_path[:MAX_EXCERPT_CHARACTERS],
                        }
                    )
                    if len(results) >= validated.max_results:
                        truncated = True
                        break

                for line_number, line in self._matching_lines(path, needle):
                    results.append(
                        {
                            "path": relative_path,
                            "match_type": "content",
                            "line": line_number,
                            "excerpt": line[:MAX_EXCERPT_CHARACTERS],
                        }
                    )
                    if len(results) >= validated.max_results:
                        truncated = True
                        break
                if len(results) >= validated.max_results:
                    break
            if truncated:
                break

        count = len(results)
        noun = "match" if count == 1 else "matches"
        return {
            "query": validated.query,
            "path": validated.path,
            "matches": results,
            "match_count": count,
            "scanned_files": scanned_files,
            "truncated": truncated,
            "summary": f'Found {count} {noun} for "{validated.query}".',
        }

    @staticmethod
    def _matching_lines(path: Path, needle: str) -> list[tuple[int, str]]:
        try:
            if path.stat().st_size > MAX_SEARCH_FILE_BYTES:
                return []
            with path.open("rb") as stream:
                payload = stream.read(MAX_SEARCH_FILE_BYTES + 1)
            if len(payload) > MAX_SEARCH_FILE_BYTES or b"\x00" in payload:
                return []
            text = payload.decode("utf-8-sig")
        except (OSError, UnicodeDecodeError):
            return []
        return [
            (line_number, line.strip())
            for line_number, line in enumerate(text.splitlines(), start=1)
            if needle in line.casefold()
        ]
