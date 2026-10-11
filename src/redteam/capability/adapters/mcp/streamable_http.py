"""Reusable capability adapter for MCP servers on supported transports."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any

from pydantic import TypeAdapter, ValidationError

from redteam.capability.adapters.mcp.base import MCPCallResult
from redteam.capability.adapters.mcp.naming import build_canonical_mcp_tool_id
from redteam.capability.adapters.mcp.transport import (
    MCPTransport,
    StreamableHTTPMCPTransport,
)
from redteam.core.contracts.tool import (
    PROVIDER_METADATA_AVAILABILITY_IMPACT,
    PROVIDER_METADATA_COMMAND_FIELDS,
    PROVIDER_METADATA_REQUIRES_TARGET,
    PROVIDER_METADATA_RESOURCE_BINDINGS,
    PROVIDER_METADATA_TARGET_BINDING,
    ToolDescriptor,
    ToolError,
    ToolResourceBinding,
    ToolTargetBinding,
)
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
                tool_id=build_canonical_mcp_tool_id(
                    self._tool_namespace, self.server_id, tool.name
                ),
                server_id=self.server_id,
                tool_name=tool.name,
                description=tool.description,
                input_schema=tool.input_schema,
                output_schema=tool.output_schema,
                capability_tags=list(self._capability_tags),
                risk_level=self._risk_level,
                health_status=health,
            )
            for tool in remote_tools
        ]
        for descriptor, remote_tool in zip(descriptors, remote_tools, strict=True):
            _apply_provider_metadata(descriptor, remote_tool.metadata)
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


def _apply_provider_metadata(
    descriptor: ToolDescriptor, metadata: Mapping[str, Any] | None
) -> None:
    # Provider-declared target semantics and command fields from the
    # shared metadata keys. Absent keys keep the contract defaults;
    # no per-tool central list lives here.
    if not metadata:
        return
    if PROVIDER_METADATA_RESOURCE_BINDINGS in metadata:
        from redteam.capability.settings import MCPConfigurationError

        try:
            descriptor.resource_bindings = TypeAdapter(list[ToolResourceBinding]).validate_python(
                metadata[PROVIDER_METADATA_RESOURCE_BINDINGS], strict=True
            )
            if len(descriptor.resource_bindings) > 32:
                raise ValueError("too many resource declarations")
        except (ValidationError, ValueError):
            raise MCPConfigurationError("invalid provider resource metadata") from None
    binding_spec = metadata.get(PROVIDER_METADATA_TARGET_BINDING)
    if isinstance(binding_spec, Mapping):
        known = {
            key: binding_spec[key] for key in binding_spec if key in ToolTargetBinding.model_fields
        }
        descriptor.target_binding = ToolTargetBinding.model_validate(known)
    command_fields = metadata.get(PROVIDER_METADATA_COMMAND_FIELDS)
    if isinstance(command_fields, (list, tuple)):
        descriptor.command_fields = [str(name) for name in command_fields if str(name)]
    availability_impact = metadata.get(PROVIDER_METADATA_AVAILABILITY_IMPACT)
    # Owner-declared bool only; any other type keeps the descriptor default
    # instead of coercing (e.g. "true"/1 must not become True).
    if isinstance(availability_impact, bool):
        descriptor.availability_impact = availability_impact
    requires_target = metadata.get(PROVIDER_METADATA_REQUIRES_TARGET)
    # Owner-declared bool only; absent keys keep the contract default and
    # any other type is ignored instead of coerced. Never inferred from
    # target binding or target types here.
    if isinstance(requires_target, bool):
        descriptor.requires_target = requires_target


def _safe_error_message(output: dict[str, Any] | str | None) -> str:
    if isinstance(output, str):
        return output[:500]
    if output is None:
        return "MCP tool reported an error"
    return "MCP tool reported an error; structured details are available in output"


# Backward-compatible name for code written before stdio transport support.
StreamableHTTPMCPAdapter = MCPServerAdapter
