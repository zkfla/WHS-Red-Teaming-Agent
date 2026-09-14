"""Default ``ToolGateway`` implementation. Owned by Capability Team.

Routes an already-approved ``ToolInvocation`` to the ``MCPAdapter`` that
owns the target tool's ``server_id``. This is the only class in the
codebase allowed to call an ``MCPAdapter`` directly.

TODO(capability-team): per-tool timeout enforcement, retries, circuit
breaking on repeated adapter failures.
"""

from __future__ import annotations

from datetime import datetime, timezone

from redteam.capability.adapters.mcp.base import MCPAdapter
from redteam.capability.registry import MCPToolRegistry
from redteam.core.contracts.tool import ToolError, ToolInvocation, ToolResult


class UnknownToolError(Exception):
    pass


class MCPToolGateway:
    def __init__(self, registry: MCPToolRegistry, adapters: dict[str, MCPAdapter]) -> None:
        self._registry = registry
        self._adapters = adapters

    async def invoke(self, invocation: ToolInvocation) -> ToolResult:
        descriptor = self._registry.get(invocation.tool_id)
        if descriptor is None:
            return ToolResult(
                invocation_id=invocation.invocation_id,
                action_id=invocation.action_id,
                tool_id=invocation.tool_id,
                success=False,
                error=ToolError(
                    code="unknown_tool", message=f"tool {invocation.tool_id} not registered"
                ),
                started_at=datetime.now(timezone.utc),
                finished_at=datetime.now(timezone.utc),
            )

        adapter = self._adapters.get(descriptor.server_id)
        if adapter is None:
            return ToolResult(
                invocation_id=invocation.invocation_id,
                action_id=invocation.action_id,
                tool_id=invocation.tool_id,
                success=False,
                error=ToolError(
                    code="unknown_server",
                    message=f"no adapter registered for server {descriptor.server_id}",
                ),
                started_at=datetime.now(timezone.utc),
                finished_at=datetime.now(timezone.utc),
            )

        result = await adapter.invoke(descriptor, invocation.arguments)
        return result.model_copy(
            update={
                "invocation_id": invocation.invocation_id,
                "action_id": invocation.action_id,
                "tool_id": invocation.tool_id,
            }
        )
