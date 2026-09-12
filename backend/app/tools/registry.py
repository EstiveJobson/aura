from typing import Any

from app.tools.base import ApprovalBoundTool, PermissionLevel, Tool, ToolDefinition


class ToolError(RuntimeError):
    """Base error for safe tool lookup, validation, and execution failures."""


class ToolApprovalRequired(ToolError):
    """Raised when application policy blocks an unapproved mutation."""


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        name = tool.definition.name
        if name in self._tools:
            raise ValueError(f'Tool "{name}" is already registered.')
        self._tools[name] = tool

    def get(self, name: str) -> Tool:
        try:
            return self._tools[name]
        except KeyError as exc:
            raise ToolError(f'Tool "{name}" is not registered.') from exc

    def definitions(self) -> tuple[ToolDefinition, ...]:
        return tuple(tool.definition for tool in self._tools.values())


class ToolExecutor:
    def __init__(self, registry: ToolRegistry) -> None:
        self._registry = registry

    def validate_tool(self, name: str) -> None:
        self._registry.get(name)

    def permission_for(self, name: str) -> PermissionLevel:
        return self._registry.get(name).definition.permission

    def requires_approval(self, name: str) -> bool:
        return self.permission_for(name) is PermissionLevel.WRITE

    def validate_call(self, name: str, arguments: dict[str, Any]) -> None:
        tool = self._registry.get(name)
        try:
            tool.validate_arguments(arguments)
        except Exception as exc:
            raise ToolError(f'Tool "{name}" received invalid arguments.') from exc

    def capture_approval_context(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        tool = self._registry.get(name)
        if tool.definition.permission is not PermissionLevel.WRITE or not isinstance(
            tool, ApprovalBoundTool
        ):
            raise ToolError(f'Tool "{name}" cannot create a safe approval context.')
        try:
            self.validate_call(name, arguments)
            return tool.capture_approval_context(arguments)
        except Exception as exc:
            raise ToolError(f'Tool "{name}" could not be prepared safely.') from exc

    def execute(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        approved: bool = False,
        approval_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        tool = self._registry.get(name)
        if tool.definition.permission is PermissionLevel.WRITE and not approved:
            raise ToolApprovalRequired(f'Tool "{name}" requires explicit approval.')
        try:
            self.validate_call(name, arguments)
            if tool.definition.permission is PermissionLevel.WRITE:
                if not isinstance(tool, ApprovalBoundTool) or approval_context is None:
                    raise ToolError(f'Tool "{name}" is missing its persisted approval context.')
                return tool.execute(arguments, approval_context=approval_context)
            return tool.execute(arguments)
        except Exception as exc:
            raise ToolError(f'Tool "{name}" could not be executed safely.') from exc
