import os
import sys
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from typing import Any, cast

import pytest
from pydantic import ValidationError

from app.tools import (
    ToolError,
    ToolExecutor,
    ToolRegistry,
    WorkspaceListTool,
    WorkspaceMoveTool,
    WorkspaceReadTool,
    WorkspaceSearchTool,
)
from app.tools.workspace_list import (
    MAX_LIST_RESULTS,
    MAX_LIST_RETURN_BYTES,
    MAX_LIST_TRAVERSAL_WORK,
    MAX_LIST_VISITED_ENTRIES,
)
from app.tools.workspace_move import WorkspaceMoveArguments
from app.tools.workspace_paths import (
    WorkspaceBoundary,
    WorkspacePathError,
    validate_relative_path,
)
from app.tools.workspace_read import WorkspaceReadArguments
from app.tools.workspace_search import (
    MAX_SCANNED_FILES,
    MAX_SEARCH_FILE_BYTES,
    MAX_SEARCH_RETURN_BYTES,
    MAX_SEARCH_TRAVERSAL_WORK,
    MAX_SEARCH_VISITED_DIRECTORIES,
    WorkspaceSearchArguments,
)


def _move_executor(root: Path) -> ToolExecutor:
    registry = ToolRegistry()
    registry.register(WorkspaceMoveTool(root))
    return ToolExecutor(registry)


def _approved_move(
    executor: ToolExecutor,
    source: str,
    destination: str,
) -> tuple[dict[str, str], dict[str, Any]]:
    arguments = {"source": source, "destination": destination}
    context = executor.capture_approval_context("workspace_move", arguments)
    return arguments, context


def _make_fifo(path: Path) -> None:
    maker = getattr(os, "mkfifo", None)
    if maker is None:
        pytest.skip("FIFOs are unavailable on this platform.")
    cast(Callable[[Path], None], maker)(path)


@pytest.mark.parametrize("path", ["", "\x00", "/absolute", "../outside", "."])
def test_portable_path_validation_rejects_unsafe_file_paths(path: str) -> None:
    with pytest.raises(WorkspacePathError):
        validate_relative_path(path, allow_root=False)
    assert validate_relative_path(".", allow_root=True) == "."


def test_workspace_boundary_rejects_exhausted_or_untrusted_internal_state(
    tmp_path: Path,
) -> None:
    boundary = WorkspaceBoundary(tmp_path)
    directory = tmp_path / "directory"
    directory.mkdir()

    assert boundary.entry_kind("missing") is None
    with pytest.raises(WorkspacePathError, match="budget is exhausted"):
        boundary.directory_batch(".", 0)
    with pytest.raises(WorkspacePathError, match="read budget is invalid"):
        boundary.read_regular_file("missing.txt", -1)
    with pytest.raises(WorkspacePathError, match="approval is invalid"):
        boundary.move_no_replace("source.txt", "moved.txt", {"model": "controlled"})
    with pytest.raises(WorkspacePathError):
        boundary.approval_context("directory")


def test_missing_workspace_root_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(WorkspacePathError):
        WorkspaceBoundary(tmp_path / "missing")


def test_source_replacement_immediately_before_acquisition_is_rejected(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.txt"
    source.write_text("approved", encoding="utf-8")
    executor = _move_executor(tmp_path)
    arguments, context = _approved_move(executor, "source.txt", "moved.txt")

    replacement = tmp_path / "replacement.txt"
    replacement.write_text("replacement", encoding="utf-8")
    os.replace(replacement, source)

    with pytest.raises(ToolError, match="could not be executed safely"):
        executor.execute(
            "workspace_move",
            arguments,
            approved=True,
            approval_context=context,
        )

    assert source.read_text(encoding="utf-8") == "replacement"
    assert not (tmp_path / "moved.txt").exists()


def test_parent_directory_replacement_is_rejected(tmp_path: Path) -> None:
    parent = tmp_path / "parent"
    parent.mkdir()
    (parent / "source.txt").write_text("approved", encoding="utf-8")
    executor = _move_executor(tmp_path)
    arguments, context = _approved_move(
        executor,
        "parent/source.txt",
        "moved.txt",
    )

    old_parent = tmp_path / "old-parent"
    parent.rename(old_parent)
    parent.mkdir()
    (parent / "source.txt").write_text("replacement", encoding="utf-8")

    with pytest.raises(ToolError, match="could not be executed safely"):
        executor.execute(
            "workspace_move",
            arguments,
            approved=True,
            approval_context=context,
        )

    assert (old_parent / "source.txt").read_text(encoding="utf-8") == "approved"
    assert (parent / "source.txt").read_text(encoding="utf-8") == "replacement"
    assert not (tmp_path / "moved.txt").exists()


def test_symlink_replacement_is_rejected_where_supported(tmp_path: Path) -> None:
    source = tmp_path / "source.txt"
    source.write_text("approved", encoding="utf-8")
    outside = tmp_path.parent / f"outside-{tmp_path.name}.txt"
    outside.write_text("outside", encoding="utf-8")
    executor = _move_executor(tmp_path)
    arguments, context = _approved_move(executor, "source.txt", "moved.txt")

    source.unlink()
    try:
        source.symlink_to(outside)
    except OSError:
        pytest.skip("Symbolic links are unavailable on this platform.")

    with pytest.raises(ToolError, match="could not be executed safely"):
        executor.execute(
            "workspace_move",
            arguments,
            approved=True,
            approval_context=context,
        )

    assert outside.read_text(encoding="utf-8") == "outside"
    assert not (tmp_path / "moved.txt").exists()


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFOs are unavailable")
def test_fifo_replacement_is_rejected_without_blocking(tmp_path: Path) -> None:
    source = tmp_path / "source.txt"
    source.write_text("approved", encoding="utf-8")
    executor = _move_executor(tmp_path)
    arguments, context = _approved_move(executor, "source.txt", "moved.txt")

    source.unlink()
    _make_fifo(source)

    with pytest.raises(ToolError, match="could not be executed safely"):
        executor.execute(
            "workspace_move",
            arguments,
            approved=True,
            approval_context=context,
        )
    assert not (tmp_path / "moved.txt").exists()


def test_destination_creation_race_never_overwrites(tmp_path: Path) -> None:
    source = tmp_path / "source.txt"
    destination = tmp_path / "moved.txt"
    source.write_text("approved", encoding="utf-8")
    executor = _move_executor(tmp_path)
    arguments, context = _approved_move(executor, "source.txt", "moved.txt")

    destination.write_text("racing owner", encoding="utf-8")

    with pytest.raises(ToolError, match="could not be executed safely"):
        executor.execute(
            "workspace_move",
            arguments,
            approved=True,
            approval_context=context,
        )
    assert source.read_text(encoding="utf-8") == "approved"
    assert destination.read_text(encoding="utf-8") == "racing owner"


def test_concurrent_moves_to_one_destination_have_exactly_one_winner(
    tmp_path: Path,
) -> None:
    (tmp_path / "first.txt").write_text("first", encoding="utf-8")
    (tmp_path / "second.txt").write_text("second", encoding="utf-8")
    executor = _move_executor(tmp_path)
    first = _approved_move(executor, "first.txt", "winner.txt")
    second = _approved_move(executor, "second.txt", "winner.txt")
    barrier = Barrier(2)

    def attempt(move: tuple[dict[str, str], dict[str, Any]]) -> bool:
        arguments, context = move
        barrier.wait()
        try:
            executor.execute(
                "workspace_move",
                arguments,
                approved=True,
                approval_context=context,
            )
        except ToolError:
            return False
        return True

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(attempt, (first, second)))

    assert outcomes.count(True) == 1
    assert outcomes.count(False) == 1
    winner = (tmp_path / "winner.txt").read_text(encoding="utf-8")
    assert winner in {"first", "second"}
    assert (tmp_path / "first.txt").exists() is (winner == "second")
    assert (tmp_path / "second.txt").exists() is (winner == "first")


def test_workspace_list_stops_before_materializing_a_large_directory(tmp_path: Path) -> None:
    for index in range(MAX_LIST_VISITED_ENTRIES + 25):
        (tmp_path / f"entry-{index:05}.txt").touch()

    result = WorkspaceListTool(tmp_path).execute({})

    assert result["truncated"] is True
    assert result["visited_entries"] <= MAX_LIST_VISITED_ENTRIES
    assert result["entry_count"] <= MAX_LIST_RESULTS
    assert result["returned_bytes"] <= MAX_LIST_RETURN_BYTES
    assert result["traversal_work"] <= MAX_LIST_TRAVERSAL_WORK


def test_workspace_search_bounds_a_large_directory(tmp_path: Path) -> None:
    for index in range(MAX_SCANNED_FILES + 25):
        (tmp_path / f"entry-{index:05}.txt").touch()

    result = WorkspaceSearchTool(tmp_path).execute(
        {"query": "not-present", "path": ".", "max_results": 10}
    )

    assert result["truncated"] is True
    assert result["scanned_files"] == MAX_SCANNED_FILES
    assert result["returned_bytes"] <= MAX_SEARCH_RETURN_BYTES
    assert result["traversal_work"] <= MAX_SEARCH_TRAVERSAL_WORK


def test_workspace_search_preserves_per_file_binary_and_size_limits(tmp_path: Path) -> None:
    (tmp_path / "binary.dat").write_bytes(b"needle\x00binary")
    (tmp_path / "invalid.dat").write_bytes(b"\xffneedle")
    (tmp_path / "oversized.dat").write_bytes(b"x" * (MAX_SEARCH_FILE_BYTES + 1))

    result = WorkspaceSearchTool(tmp_path).execute(
        {"query": "needle", "path": ".", "max_results": 10}
    )

    assert result["matches"] == []
    assert result["scanned_files"] == 3
    assert result["truncated"] is False


def test_workspace_search_bounds_trees_with_many_empty_directories(tmp_path: Path) -> None:
    for index in range(MAX_SEARCH_VISITED_DIRECTORIES + 5):
        (tmp_path / f"directory-{index:05}").mkdir()

    result = WorkspaceSearchTool(tmp_path).execute(
        {"query": "not-present", "path": ".", "max_results": 10}
    )

    assert result["truncated"] is True
    assert result["visited_directories"] == MAX_SEARCH_VISITED_DIRECTORIES
    assert result["traversal_work"] <= MAX_SEARCH_TRAVERSAL_WORK


@pytest.mark.skipif(os.name != "nt", reason="Native Windows path rules only")
@pytest.mark.parametrize(
    "path",
    [
        "CON",
        "con.txt",
        "CONOUT$",
        "folder/PRN.log",
        "folder/file.txt:secret",
        "folder/trailing.",
        "folder/trailing ",
        "COM1/config.txt",
        "LPT¹.txt",
        "C:\\outside.txt",
    ],
)
def test_windows_special_paths_are_rejected(path: str) -> None:
    with pytest.raises(ValidationError):
        WorkspaceReadArguments(path=path)
    with pytest.raises(ValidationError):
        WorkspaceSearchArguments(query="safe", path=path, max_results=1)
    with pytest.raises(ValidationError):
        WorkspaceMoveArguments(source="normal.txt", destination=path)


@pytest.mark.skipif(os.name != "nt", reason="Native Windows path rules only")
def test_windows_normal_relative_paths_remain_valid() -> None:
    assert validate_relative_path("docs/normal file.txt", allow_root=False) == (
        "docs/normal file.txt"
    )


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux path semantics only")
def test_linux_does_not_apply_windows_filename_semantics() -> None:
    assert validate_relative_path("CON", allow_root=False) == "CON"
    assert validate_relative_path("notes.", allow_root=False) == "notes."
    assert validate_relative_path("file:stream", allow_root=False) == "file:stream"


def test_workspace_read_rejects_a_special_file_where_supported(tmp_path: Path) -> None:
    if not hasattr(os, "mkfifo"):
        pytest.skip("Special files are unavailable on this platform.")
    fifo = tmp_path / "pipe"
    _make_fifo(fifo)

    with pytest.raises(ValueError, match="not a regular file"):
        WorkspaceReadTool(tmp_path).execute({"path": "pipe"})
