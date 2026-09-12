from typing import Any

from app.tools.base import Tool, ToolDefinition


class ToolError(RuntimeError):
    """Base error for safe tool lookup, validation, and execution failures."""


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

    def validate_call(self, name: str, arguments: dict[str, Any]) -> None:
        tool = self._registry.get(name)
        try:
            tool.validate_arguments(arguments)
        except Exception as exc:
            raise ToolError(f'Tool "{name}" received invalid arguments.') from exc

    def execute(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        tool = self._registry.get(name)
        try:
            return tool.execute(arguments)
        except Exception as exc:
            raise ToolError(f'Tool "{name}" could not be executed safely.') from exc
