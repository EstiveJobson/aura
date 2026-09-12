from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from app.agents import AgentEngine, GeneratedPlan, MockPlanner, PlannedStep
from app.core.constraints import PLANNER_NAME_MAX_LENGTH, TOOL_NAME_MAX_LENGTH
from app.tools import (
    MAX_READ_BYTES,
    PermissionLevel,
    ToolError,
    ToolExecutor,
    ToolRegistry,
    WorkspaceListTool,
    WorkspaceMoveTool,
    WorkspaceReadTool,
    WorkspaceSearchTool,
)


def test_mock_planner_is_deterministic_and_engine_applies_planner_identity(
    tmp_path: Path,
) -> None:
    planner = MockPlanner()
    registry = ToolRegistry()
    registry.register(WorkspaceListTool(tmp_path))

    first = planner.create_plan("List this workspace.")
    second = planner.create_plan("List this workspace.")
    execution_plan = AgentEngine(
        planner,
        ToolExecutor(registry),
        planner_name="application-owned-planner",
    ).create_plan("List this workspace.")

    assert first == second
    assert len(first.steps) == 1
    assert first.steps[0].tool_name == "workspace_list"
    assert first.steps[0].arguments == {}
    assert execution_plan.planner == "application-owned-planner"


def test_plan_and_tool_name_validation_match_storage_limits_and_keep_one_step() -> None:
    step = PlannedStep(sequence=1, title="Valid", tool_name="tool", arguments={})

    with pytest.raises(ValidationError):
        PlannedStep(
            sequence=1,
            title="Too long",
            tool_name="x" * (TOOL_NAME_MAX_LENGTH + 1),
            arguments={},
        )
    with pytest.raises(ValidationError):
        GeneratedPlan.model_validate(
            {
                "summary": "No extra steps",
                "steps": [step.model_dump(), step.model_copy(update={"sequence": 2}).model_dump()],
            }
        )

    registry = ToolRegistry()
    with pytest.raises(ValidationError):
        AgentEngine(
            MockPlanner(),
            ToolExecutor(registry),
            planner_name="x" * (PLANNER_NAME_MAX_LENGTH + 1),
        )


def test_registry_executes_only_registered_read_only_workspace_tool(tmp_path: Path) -> None:
    (tmp_path / "zeta.txt").write_text("zeta", encoding="utf-8")
    (tmp_path / "Alpha").mkdir()
    registry = ToolRegistry()
    tool = WorkspaceListTool(tmp_path)
    registry.register(tool)

    result = ToolExecutor(registry).execute("workspace_list", {})

    assert [definition.name for definition in registry.definitions()] == ["workspace_list"]
    assert registry.definitions()[0].permission is PermissionLevel.READ
    assert result["entry_count"] == 2
    assert result["entries"] == [
        {"name": "Alpha", "kind": "directory"},
        {"name": "zeta.txt", "kind": "file"},
    ]


def test_registry_rejects_duplicate_and_unknown_tools(tmp_path: Path) -> None:
    registry = ToolRegistry()
    tool = WorkspaceListTool(tmp_path)
    registry.register(tool)

    with pytest.raises(ValueError, match="already registered"):
        registry.register(tool)
    with pytest.raises(ToolError, match="not registered"):
        ToolExecutor(registry).execute("missing", {})
    with pytest.raises(ToolError, match="received invalid arguments"):
        ToolExecutor(registry).validate_call("workspace_list", {"path": "../outside"})
    with pytest.raises(ToolError, match="could not be executed safely"):
        ToolExecutor(registry).execute("workspace_list", {"unexpected": True})


def test_workspace_search_is_literal_recursive_and_bounded(tmp_path: Path) -> None:
    nested = tmp_path / "docs"
    nested.mkdir()
    (nested / "AuraNotes.txt").write_text(
        "AURA appears here.\nAnd aura appears again.", encoding="utf-8"
    )
    (nested / "other.txt").write_text("no match", encoding="utf-8")
    tool = WorkspaceSearchTool(tmp_path)

    result = tool.execute({"query": "aura", "path": ".", "max_results": 2})

    assert result["match_count"] == 2
    assert result["truncated"] is True
    assert result["matches"][0] == {
        "path": "docs/AuraNotes.txt",
        "match_type": "name",
        "line": None,
        "excerpt": "docs/AuraNotes.txt",
    }
    assert result["matches"][1]["match_type"] == "content"
    assert result["matches"][1]["line"] == 1


def test_workspace_read_returns_utf8_and_rejects_binary_and_oversized_files(
    tmp_path: Path,
) -> None:
    (tmp_path / "notes.txt").write_text("hello AURA", encoding="utf-8")
    (tmp_path / "binary.dat").write_bytes(b"AURA\x00binary")
    (tmp_path / "large.txt").write_bytes(b"x" * (MAX_READ_BYTES + 1))
    tool = WorkspaceReadTool(tmp_path)

    result = tool.execute({"path": "notes.txt"})

    assert result["path"] == "notes.txt"
    assert result["content"] == "hello AURA"
    with pytest.raises(ValueError, match="binary or unsupported"):
        tool.execute({"path": "binary.dat"})
    with pytest.raises(ValueError, match="exceeds the read limit"):
        tool.execute({"path": "large.txt"})


@pytest.mark.parametrize(
    ("tool_name", "arguments"),
    [
        ("workspace_search", {"query": "secret", "path": "../outside", "max_results": 5}),
        ("workspace_read", {"path": "../outside.txt"}),
        (
            "workspace_move",
            {"source": "../outside.txt", "destination": "inside.txt"},
        ),
        (
            "workspace_move",
            {"source": "inside.txt", "destination": "C:\\outside.txt"},
        ),
    ],
)
def test_workspace_tools_reject_traversal_and_absolute_paths(
    tmp_path: Path,
    tool_name: str,
    arguments: dict[str, Any],
) -> None:
    (tmp_path / "inside.txt").write_text("inside", encoding="utf-8")
    registry = ToolRegistry()
    registry.register(WorkspaceSearchTool(tmp_path))
    registry.register(WorkspaceReadTool(tmp_path))
    registry.register(WorkspaceMoveTool(tmp_path))

    with pytest.raises(ToolError, match="received invalid arguments"):
        ToolExecutor(registry).validate_call(tool_name, arguments)


def test_workspace_move_requires_approval_and_never_overwrites(tmp_path: Path) -> None:
    (tmp_path / "source.txt").write_text("source", encoding="utf-8")
    (tmp_path / "existing.txt").write_text("existing", encoding="utf-8")
    registry = ToolRegistry()
    registry.register(WorkspaceMoveTool(tmp_path))
    executor = ToolExecutor(registry)

    assert executor.requires_approval("workspace_move") is True
    with pytest.raises(ToolError, match="requires explicit approval"):
        executor.execute(
            "workspace_move",
            {"source": "source.txt", "destination": "moved.txt"},
        )
    assert (tmp_path / "source.txt").exists()

    with pytest.raises(ToolError, match="could not be executed safely"):
        executor.execute(
            "workspace_move",
            {"source": "source.txt", "destination": "existing.txt"},
            approved=True,
        )
    assert (tmp_path / "existing.txt").read_text(encoding="utf-8") == "existing"

    result = executor.execute(
        "workspace_move",
        {"source": "source.txt", "destination": "moved.txt"},
        approved=True,
    )
    assert result["destination"] == "moved.txt"
    assert not (tmp_path / "source.txt").exists()
    assert (tmp_path / "moved.txt").read_text(encoding="utf-8") == "source"


@pytest.mark.parametrize("tool_kind", ["search", "read", "move"])
def test_workspace_tools_reject_symlink_escape_where_supported(
    tmp_path: Path,
    tool_kind: str,
) -> None:
    outside = tmp_path.parent / f"outside-{tmp_path.name}.txt"
    outside.write_text("secret outside", encoding="utf-8")
    link = tmp_path / "escape.txt"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("Symbolic links are not available on this platform.")

    registry = ToolRegistry()
    if tool_kind == "search":
        registry.register(WorkspaceSearchTool(tmp_path))
        result = ToolExecutor(registry).execute(
            "workspace_search",
            {"query": "secret", "path": ".", "max_results": 5},
        )
        assert result["matches"] == []
    elif tool_kind == "read":
        registry.register(WorkspaceReadTool(tmp_path))
        with pytest.raises(ToolError, match="could not be executed safely"):
            ToolExecutor(registry).execute("workspace_read", {"path": "escape.txt"})
    else:
        registry.register(WorkspaceMoveTool(tmp_path))
        with pytest.raises(ToolError, match="could not be executed safely"):
            ToolExecutor(registry).execute(
                "workspace_move",
                {"source": "escape.txt", "destination": "moved.txt"},
                approved=True,
            )

    assert outside.read_text(encoding="utf-8") == "secret outside"
