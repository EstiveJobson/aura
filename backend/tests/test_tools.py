from pathlib import Path

import pytest
from pydantic import ValidationError

from app.agents import AgentEngine, GeneratedPlan, MockPlanner, PlannedStep
from app.core.constraints import PLANNER_NAME_MAX_LENGTH, TOOL_NAME_MAX_LENGTH
from app.tools import PermissionLevel, ToolError, ToolExecutor, ToolRegistry, WorkspaceListTool


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
