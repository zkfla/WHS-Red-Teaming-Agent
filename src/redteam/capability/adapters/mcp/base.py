"""MCP Adapter Protocol. Owned by Capability Team.

Every MCP server the framework talks to gets one adapter implementing
this Protocol — no other part of the codebase changes when a new MCP
server is added or an existing one is swapped out.
"""

from __future__ import annotations

from typing import Protocol

from redteam.core.contracts.tool import ToolDescriptor, ToolResult
from redteam.core.enums import ToolHealthStatus


class MCPAdapter(Protocol):
    async def discover_tools(self) -> list[ToolDescriptor]: ...

    async def invoke(self, tool: ToolDescriptor, arguments: dict) -> ToolResult: ...

    async def health_check(self) -> ToolHealthStatus: ...
