"""MCP Tool Registry. Owned by Capability Team.

Tracks which ``ToolDescriptor``s exist and which MCP server (adapter)
serves each one. In-memory for now; TODO(capability-team): persist +
periodic health re-checks.
"""

from __future__ import annotations

from redteam.core.contracts.tool import ToolDescriptor
from redteam.core.ids import ToolId


class MCPToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[ToolId, ToolDescriptor] = {}

    def register(self, descriptor: ToolDescriptor) -> None:
        self._tools[descriptor.tool_id] = descriptor

    def get(self, tool_id: ToolId) -> ToolDescriptor | None:
        return self._tools.get(tool_id)

    def list_by_capability(self, capability_tag: str) -> list[ToolDescriptor]:
        return [t for t in self._tools.values() if capability_tag in t.capability_tags]

    def all(self) -> list[ToolDescriptor]:
        return list(self._tools.values())
