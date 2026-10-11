"""MCP Adapter Protocol. Owned by Capability Team.

Every MCP server the framework talks to gets one adapter implementing
this Protocol — no other part of the codebase changes when a new MCP
server is added or an existing one is swapped out.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from redteam.core.contracts.tool import ToolDescriptor, ToolError, ToolInvocation
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


@runtime_checkable
class InvocationContextBinder(Protocol):
    """Optional provider contract for arguments bound to trusted execution identity.

    Providers own the identity fields and mismatch rules. The gateway passes
    the approved invocation and validates the returned arguments against the
    same descriptor; ordinary adapters need not implement this contract.
    """

    def bind_context(self, invocation: ToolInvocation) -> dict[str, Any]: ...


@runtime_checkable
class ContextualInvocationAdapter(Protocol):
    """Optional execution contract for providers that persist correlation.

    The gateway supplies the same approved invocation with bound, validated
    arguments after admission. Providers can associate durable execution
    handles with trusted IDs without exposing identity fields as model input
    or retaining a mutable binding cache. Legacy adapters keep invoke().
    """

    async def invoke_contextual(self, invocation: ToolInvocation) -> MCPCallResult: ...
