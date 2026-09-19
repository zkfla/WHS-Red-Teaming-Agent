"""Default ``ToolGateway`` implementation. Owned by Capability Team.

Routes an already-approved ``ToolInvocation`` to the ``MCPAdapter`` that
owns the target tool's ``server_id``. This is the only class in the
codebase allowed to call an ``MCPAdapter`` directly.

Retries remain deliberately disabled because a generic gateway cannot
assume that a tool invocation is idempotent.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Collection, Mapping
from datetime import datetime, timezone
from typing import Any

from redteam.capability.adapters.mcp.base import MCPAdapter, MCPCallResult
from redteam.capability.catalog import MCPToolCatalog
from redteam.capability.registry import MCPToolRegistry
from redteam.core.contracts.tool import ToolDescriptor, ToolError, ToolInvocation, ToolResult
from redteam.core.enums import ToolHealthStatus
from redteam.core.ids import ToolId


class MCPToolGateway:
    def __init__(
        self,
        registry: MCPToolRegistry,
        adapters: dict[str, MCPAdapter],
        operation_bindings: Mapping[str, Collection[str]] | None = None,
        *,
        server_concurrency: Mapping[str, int] | None = None,
        server_output_limits: Mapping[str, int] | None = None,
    ) -> None:
        self._registry = registry
        self._adapters = adapters
        self._catalog = MCPToolCatalog(registry, operation_bindings)
        concurrency = server_concurrency or {}
        unknown_servers = set(concurrency) - set(adapters)
        if unknown_servers:
            names = ", ".join(sorted(unknown_servers))
            raise ValueError(f"concurrency configured for unknown MCP servers: {names}")
        if any(limit < 1 for limit in concurrency.values()):
            raise ValueError("MCP server concurrency must be at least 1")
        self._server_semaphores = {
            server_id: asyncio.Semaphore(concurrency.get(server_id, 1))
            for server_id in adapters
        }
        output_limits = server_output_limits or {}
        unknown_output_servers = set(output_limits) - set(adapters)
        if unknown_output_servers:
            names = ", ".join(sorted(unknown_output_servers))
            raise ValueError(f"output limit configured for unknown MCP servers: {names}")
        if any(limit < 1 for limit in output_limits.values()):
            raise ValueError("MCP server output limit must be at least 1 byte")
        self._server_output_limits = {
            server_id: output_limits.get(server_id, 1_048_576) for server_id in adapters
        }

    def get(self, tool_id: ToolId) -> ToolDescriptor | None:
        """Expose a defensive catalog snapshot to the application composition root."""
        return self._registry.get(tool_id)

    def list_by_capability(self, capability_tag: str) -> list[ToolDescriptor]:
        return self._registry.list_by_capability(capability_tag)

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

        if not self._catalog.supports(invocation.tool_id, invocation.operation):
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

        if descriptor.health_status == ToolHealthStatus.UNHEALTHY:
            return self._error_result(
                invocation,
                code="tool_unhealthy",
                message=f"tool {invocation.tool_id} belongs to an unhealthy MCP server",
                retryable=True,
            )

        try:
            adapter_result = await asyncio.wait_for(
                _invoke_with_limit(
                    adapter,
                    descriptor,
                    invocation.arguments,
                    self._server_semaphores[descriptor.server_id],
                ),
                timeout=invocation.timeout_seconds,
            )
        except asyncio.TimeoutError:
            return self._error_result(
                invocation,
                code="tool_timeout",
                message=(
                    "tool queue and execution exceeded timeout of "
                    f"{invocation.timeout_seconds} seconds"
                ),
                retryable=True,
            )
        except Exception as exc:
            return self._error_result(
                invocation,
                code="adapter_error",
                message=f"adapter execution failed: {type(exc).__name__}",
            )

        output_bytes = _serialized_output_size(adapter_result.output)
        max_output_bytes = self._server_output_limits[descriptor.server_id]
        if output_bytes > max_output_bytes:
            return ToolResult(
                invocation_id=invocation.invocation_id,
                action_id=invocation.action_id,
                tool_id=invocation.tool_id,
                success=False,
                output=None,
                error=ToolError(
                    code="tool_output_too_large",
                    message="tool output exceeded the configured byte limit and was omitted",
                    retryable=False,
                ),
                started_at=adapter_result.started_at,
                finished_at=adapter_result.finished_at,
                metadata={
                    "output_omitted": True,
                    "output_bytes": output_bytes,
                    "max_output_bytes": max_output_bytes,
                },
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

    def list_tools_for_action(
        self,
        capability: str,
        operation: str,
        *,
        include_unhealthy: bool = False,
    ) -> list[ToolDescriptor]:
        """Return eligible Registry candidates without applying selection policy."""

        return self._catalog.list_tools_for_action(
            capability,
            operation,
            include_unhealthy=include_unhealthy,
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


async def _invoke_with_limit(
    adapter: MCPAdapter,
    descriptor: ToolDescriptor,
    arguments: dict[str, Any],
    semaphore: asyncio.Semaphore,
) -> MCPCallResult:
    async with semaphore:
        return await adapter.invoke(descriptor, arguments)


def _serialized_output_size(output: dict[str, Any] | str | None) -> int:
    if output is None:
        return 0
    if isinstance(output, str):
        return len(output.encode("utf-8"))
    serialized = json.dumps(
        output,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        default=str,
    )
    return len(serialized.encode("utf-8"))
