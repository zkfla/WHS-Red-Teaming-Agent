"""MCP Adapter Protocol. Owned by Capability Team.

Every MCP server the framework talks to gets one adapter implementing
this Protocol — no other part of the codebase changes when a new MCP
server is added or an existing one is swapped out.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from redteam.core.contracts.tool import ToolDescriptor, ToolError
from redteam.core.enums import ToolHealthStatus


@dataclass(frozen=True, slots=True)
class MCPCallResult:
    """Adapter-local result before framework correlation is attached.

    MCP servers do not know framework ``InvocationId``/``ActionId`` values.
    Adapters return transport/tool data and the gateway builds the public
    ``ToolResult`` at the Capability boundary.
    """

    success: bool
    started_at: datetime
    finished_at: datetime
    output: dict[str, Any] | str | None = None
    error: ToolError | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class MCPAdapter(Protocol):
    """Internal contract implemented once per MCP server or server class."""

    server_id: str

    async def discover_tools(self) -> list[ToolDescriptor]: ...

    async def invoke(
        self, tool: ToolDescriptor, arguments: dict[str, Any]
    ) -> MCPCallResult: ...

    async def health_check(self) -> ToolHealthStatus: ...
