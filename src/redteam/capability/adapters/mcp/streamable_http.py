"""Reusable capability adapter for MCP servers on supported transports."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any

from redteam.capability.adapters.mcp.base import MCPCallResult
from redteam.capability.adapters.mcp.naming import build_mcp_tool_id
from redteam.capability.adapters.mcp.transport import (
    MCPTransport,
    StreamableHTTPMCPTransport,
)
from redteam.core.contracts.tool import ToolDescriptor, ToolError
from redteam.core.enums import RiskLevel, ToolHealthStatus
from redteam.core.ids import ToolId


class MCPServerAdapter:
    """Maps one remote MCP server into framework Capability contracts."""

    def __init__(
        self,
        *,
        server_id: str,
        endpoint: str | None = None,
        tool_namespace: str,
        capability_tags: tuple[str, ...],
        risk_level: RiskLevel = RiskLevel.LOW,
        headers: Mapping[str, str] | None = None,
        transport: MCPTransport | None = None,
    ) -> None:
        if not server_id.strip():
            raise ValueError("server_id must not be empty")
        if not capability_tags:
            raise ValueError("at least one capability tag is required")
        self.server_id = server_id
        self._tool_namespace = tool_namespace
        self._capability_tags = capability_tags
        self._risk_level = risk_level
        if transport is None:
            if endpoint is None:
                raise ValueError("endpoint is required when transport is not provided")
            transport = StreamableHTTPMCPTransport(endpoint, headers=headers)
        self._transport = transport
        self._discovered_output_schemas: dict[ToolId, dict[str, Any]] = {}

    async def discover_tools(self) -> list[ToolDescriptor]:
        health = await self.health_check()
        remote_tools = await self._transport.list_tools()
        descriptors = [
            ToolDescriptor(
                tool_id=build_mcp_tool_id(self._tool_namespace, tool.name),
                server_id=self.server_id,
                tool_name=tool.name,
                description=tool.description,
                input_schema=tool.input_schema,
                capability_tags=list(self._capability_tags),
                risk_level=self._risk_level,
                health_status=health,
            )
            for tool in remote_tools
        ]
        self._discovered_output_schemas = {
            descriptor.tool_id: deepcopy(remote_tool.output_schema)
            for descriptor, remote_tool in zip(descriptors, remote_tools, strict=True)
            if remote_tool.output_schema is not None
        }
        return descriptors

    def output_schema_for_tool(self, tool_id: ToolId) -> dict[str, Any] | None:
        """Return a discovered MCP output schema without changing the Core contract."""
        schema = self._discovered_output_schemas.get(tool_id)
        return deepcopy(schema) if schema is not None else None

    async def invoke(self, tool: ToolDescriptor, arguments: dict[str, Any]) -> MCPCallResult:
        if tool.server_id != self.server_id:
            raise ValueError(
                f"tool belongs to server {tool.server_id}, not adapter {self.server_id}"
            )

        started_at = datetime.now(timezone.utc)
        result = await self._transport.call_tool(tool.tool_name, arguments)
        finished_at = datetime.now(timezone.utc)
        error = None
        if result.is_error:
            error = ToolError(
                code="mcp_tool_error",
                message=_safe_error_message(result.output),
                retryable=False,
            )
        return MCPCallResult(
            success=not result.is_error,
            output=result.output,
            error=error,
            started_at=started_at,
            finished_at=finished_at,
            metadata={"mcp_server_id": self.server_id, "mcp_tool_name": tool.tool_name},
        )

    async def health_check(self) -> ToolHealthStatus:
        healthy = await self._transport.health_check()
        return ToolHealthStatus.HEALTHY if healthy else ToolHealthStatus.UNHEALTHY


def _safe_error_message(output: dict[str, Any] | str | None) -> str:
    if isinstance(output, str):
        return output[:500]
    if output is None:
        return "MCP tool reported an error"
    return "MCP tool reported an error; structured details are available in output"


# Backward-compatible name for code written before stdio transport support.
StreamableHTTPMCPAdapter = MCPServerAdapter
