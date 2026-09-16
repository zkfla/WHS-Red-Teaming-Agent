"""Default ``ToolGateway`` implementation. Owned by Capability Team.

Routes an already-approved ``ToolInvocation`` to the ``MCPAdapter`` that
owns the target tool's ``server_id``. This is the only class in the
codebase allowed to call an ``MCPAdapter`` directly.

Retries remain deliberately disabled because a generic gateway cannot
assume that a tool invocation is idempotent.
"""

from __future__ import annotations

import asyncio
from collections.abc import Collection, Mapping
from datetime import datetime, timezone

from redteam.capability.adapters.mcp.base import MCPAdapter
from redteam.capability.registry import MCPToolRegistry
from redteam.core.contracts.tool import ToolError, ToolInvocation, ToolResult


class MCPToolGateway:
    def __init__(
        self,
        registry: MCPToolRegistry,
        adapters: dict[str, MCPAdapter],
        operation_bindings: Mapping[str, Collection[str]] | None = None,
    ) -> None:
        self._registry = registry
        self._adapters = adapters
        self._operation_bindings = (
            {
                str(tool_id): frozenset(operations)
                for tool_id, operations in operation_bindings.items()
            }
            if operation_bindings is not None
            else None
        )

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

        allowed_operations = (
            self._operation_bindings.get(str(invocation.tool_id), frozenset())
            if self._operation_bindings is not None
            else frozenset((descriptor.tool_name,))
        )
        if invocation.operation not in allowed_operations:
            return self._error_result(
                invocation,
                code="operation_not_allowed_for_tool",
                message=(
                    f"tool {invocation.tool_id} is not registered for canonical operation "
                    f"{invocation.operation!r}"
                ),
            )

        if invocation.execution_context.dry_run:
            occurred_at = datetime.now(timezone.utc)
            return ToolResult(
                invocation_id=invocation.invocation_id, action_id=invocation.action_id,
                tool_id=invocation.tool_id, success=True,
                output={"dry_run": True, "executed": False},
                started_at=occurred_at, finished_at=occurred_at,
                metadata={"dry_run": True},
            )

        try:
            adapter_result = await asyncio.wait_for(
                adapter.invoke(descriptor, invocation.arguments),
                timeout=invocation.timeout_seconds,
            )
        except asyncio.TimeoutError:
            return self._error_result(
                invocation,
                code="tool_timeout",
                message=f"tool exceeded timeout of {invocation.timeout_seconds} seconds",
                retryable=True,
            )
        except Exception as exc:
            return self._error_result(
                invocation,
                code="adapter_error",
                message=f"adapter execution failed: {type(exc).__name__}",
            )

        return ToolResult(
            invocation_id=invocation.invocation_id,
            action_id=invocation.action_id,
            tool_id=invocation.tool_id,
            success=adapter_result.success,
            output=adapter_result.output,
            error=adapter_result.error,
            started_at=adapter_result.started_at,
            finished_at=adapter_result.finished_at,
            metadata=adapter_result.metadata,
        )

    @staticmethod
    def _error_result(
        invocation: ToolInvocation,
        *,
        code: str,
        message: str,
        retryable: bool = False,
    ) -> ToolResult:
        occurred_at = datetime.now(timezone.utc)
        return ToolResult(
            invocation_id=invocation.invocation_id,
            action_id=invocation.action_id,
            tool_id=invocation.tool_id,
            success=False,
            error=ToolError(code=code, message=message, retryable=retryable),
            started_at=occurred_at,
            finished_at=occurred_at,
        )
