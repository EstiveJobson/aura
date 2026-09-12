from collections import deque
from collections.abc import Iterator
from pathlib import Path, PurePosixPath
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.tools.base import PermissionLevel, ToolDefinition
from app.tools.workspace_paths import (
    WorkspaceBoundary,
    WorkspaceFileTooLarge,
    WorkspacePathError,
    serialized_item_size,
    validate_relative_path,
)

MAX_SEARCH_RESULTS = 100
MAX_SCANNED_FILES = 2_000
MAX_SEARCH_FILE_BYTES = 128 * 1024
MAX_EXCERPT_CHARACTERS = 240
MAX_SEARCH_VISITED_ENTRIES = 10_000
MAX_SEARCH_VISITED_DIRECTORIES = 1_000
MAX_SEARCH_DEPTH = 32
MAX_SEARCH_RETURN_BYTES = 128 * 1024
MAX_SEARCH_TOTAL_FILE_BYTES = 8 * 1024 * 1024
MAX_SEARCH_TRAVERSAL_WORK = MAX_SEARCH_VISITED_ENTRIES + MAX_SEARCH_VISITED_DIRECTORIES


class WorkspaceSearchArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

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
        normalized = value.strip()
        if not normalized or "\x00" in normalized:
            raise ValueError("The search query is invalid.")
        return normalized

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return validate_relative_path(value, allow_root=True)


class WorkspaceSearchTool:
    """Perform a literal search with explicit traversal and output budgets."""

    def __init__(self, workspace_root: Path) -> None:
        self._boundary = WorkspaceBoundary(workspace_root)

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="workspace_search",
            description=(
                "Search file names and UTF-8 text content below a workspace-relative directory. "
                "Traversal depth, entries, directories, files, bytes, results, and excerpts are "
                "bounded."
            ),
            permission=PermissionLevel.READ,
            parameters=WorkspaceSearchArguments.model_json_schema(),
        )

    def validate_arguments(self, arguments: dict[str, Any]) -> None:
        WorkspaceSearchArguments.model_validate(arguments)

    def execute(self, arguments: dict[str, Any]) -> dict[str, Any]:
        validated = WorkspaceSearchArguments.model_validate(arguments)
        if self._boundary.entry_kind(validated.path) != "directory":
            raise ValueError("The requested search path is not a directory.")

        needle = validated.query.casefold()
        results: list[dict[str, Any]] = []
        returned_bytes = 0
        scanned_files = 0
        scanned_file_bytes = 0
        visited_entries = 0
        visited_directories = 0
        truncated = False
        stop = False
        pending: deque[tuple[str, int]] = deque([(validated.path, 0)])

        while pending and not stop:
            if visited_directories >= MAX_SEARCH_VISITED_DIRECTORIES:
                truncated = True
                break
            directory, depth = pending.popleft()
            visited_directories += 1
            remaining_entries = MAX_SEARCH_VISITED_ENTRIES - visited_entries
            remaining_work = MAX_SEARCH_TRAVERSAL_WORK - (visited_entries + visited_directories)
            visit_budget = min(remaining_entries, remaining_work)
            if visit_budget < 1:
                truncated = True
                break

            try:
                batch = self._boundary.directory_batch(directory, visit_budget)
            except WorkspacePathError:
                if directory == validated.path:
                    raise
                continue
            visited_entries += batch.visited_entries
            truncated = truncated or batch.truncated

            for name in batch.names:
                relative_path = self._join(directory, name)
                kind = self._boundary.entry_kind(relative_path)
                if kind == "directory":
                    if depth >= MAX_SEARCH_DEPTH:
                        truncated = True
                    else:
                        pending.append((relative_path, depth + 1))
                    continue
                if kind != "file":
                    continue
                if scanned_files >= MAX_SCANNED_FILES:
                    truncated = True
                    stop = True
                    break

                scanned_files += 1
                name_match = {
                    "path": relative_path,
                    "match_type": "name",
                    "line": None,
                    "excerpt": relative_path[:MAX_EXCERPT_CHARACTERS],
                }
                if needle in relative_path.casefold():
                    accepted, item_bytes = self._accept_match(
                        name_match,
                        results,
                        returned_bytes,
                        validated.max_results,
                    )
                    if not accepted:
                        truncated = True
                        stop = True
                        break
                    returned_bytes += item_bytes

                remaining_file_bytes = MAX_SEARCH_TOTAL_FILE_BYTES - scanned_file_bytes
                if remaining_file_bytes < 2:
                    truncated = True
                    stop = True
                    break
                read_limit = min(MAX_SEARCH_FILE_BYTES, remaining_file_bytes - 1)
                try:
                    payload = self._boundary.read_regular_file(relative_path, read_limit)
                except WorkspaceFileTooLarge:
                    if read_limit < MAX_SEARCH_FILE_BYTES:
                        truncated = True
                        stop = True
                        break
                    continue
                except WorkspacePathError:
                    continue
                scanned_file_bytes += len(payload)
                if b"\x00" in payload:
                    continue
                try:
                    text = payload.decode("utf-8-sig")
                except UnicodeDecodeError:
                    continue

                for line_number, line in self._matching_lines(text, needle):
                    content_match = {
                        "path": relative_path,
                        "match_type": "content",
                        "line": line_number,
                        "excerpt": line[:MAX_EXCERPT_CHARACTERS],
                    }
                    accepted, item_bytes = self._accept_match(
                        content_match,
                        results,
                        returned_bytes,
                        validated.max_results,
                    )
                    if not accepted:
                        truncated = True
                        stop = True
                        break
                    returned_bytes += item_bytes
                if stop:
                    break

            if batch.truncated:
                break

        count = len(results)
        noun = "match" if count == 1 else "matches"
        qualifier = " (truncated)" if truncated else ""
        return {
            "query": validated.query,
            "path": validated.path,
            "matches": results,
            "match_count": count,
            "scanned_files": scanned_files,
            "scanned_file_bytes": scanned_file_bytes,
            "visited_entries": visited_entries,
            "visited_directories": visited_directories,
            "max_depth": MAX_SEARCH_DEPTH,
            "returned_bytes": returned_bytes,
            "traversal_work": visited_entries + visited_directories,
            "truncated": truncated,
            "summary": f'Found {count} {noun} for "{validated.query}"{qualifier}.',
        }

    @staticmethod
    def _join(directory: str, name: str) -> str:
        if directory == ".":
            return name
        return (PurePosixPath(directory) / name).as_posix()

    @staticmethod
    def _matching_lines(text: str, needle: str) -> Iterator[tuple[int, str]]:
        for line_number, line in enumerate(text.splitlines(), start=1):
            stripped = line.strip()
            if needle in stripped.casefold():
                yield line_number, stripped

    @staticmethod
    def _accept_match(
        item: dict[str, Any],
        results: list[dict[str, Any]],
        returned_bytes: int,
        max_results: int,
    ) -> tuple[bool, int]:
        item_bytes = serialized_item_size(item)
        if len(results) >= max_results or returned_bytes + item_bytes > MAX_SEARCH_RETURN_BYTES:
            return False, 0
        results.append(item)
        return True, item_bytes
