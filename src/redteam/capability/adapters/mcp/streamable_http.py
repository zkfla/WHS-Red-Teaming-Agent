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
    EXECUTION_TERMINATED_METADATA_KEY,
    EXECUTION_UNCERTAIN_METADATA_KEY,
    PROVIDER_METADATA_AVAILABILITY_IMPACT,
    PROVIDER_METADATA_COMMAND_FIELDS,
    PROVIDER_METADATA_REQUIRES_TARGET,
    PROVIDER_METADATA_RESOURCE_BINDINGS,
    PROVIDER_METADATA_RESOURCE_EFFECTS,
    PROVIDER_METADATA_TARGET_BINDING,
    PROVIDER_RESULT_EXECUTION_TERMINATED,
    PROVIDER_RESULT_EXECUTION_UNCERTAIN,
    ToolDescriptor,
    ToolError,
    ToolResourceBinding,
    ToolResourceEffect,
    ToolTargetBinding,
)
from redteam.core.enums import RiskLevel, ToolHealthStatus
from redteam.core.ids import ToolId
from redteam.core.redaction import redact_sensitive_text


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
        trust_execution_termination: bool = False,
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
        self._trust_execution_termination = trust_execution_termination is True
        self._discovered_output_schemas: dict[ToolId, dict[str, Any]] = {}

    def configure_execution_termination_trust(self, trusted: bool) -> None:
        """Set assembly-verified trust before tool discovery and invocation."""
        if not isinstance(trusted, bool):
            raise ValueError("execution termination trust must be a bool")
        self._trust_execution_termination = trusted

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
            apply_provider_metadata(descriptor, remote_tool.metadata)
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
        provider_metadata: dict[str, Any] = {
            "mcp_server_id": self.server_id, "mcp_tool_name": tool.tool_name,
            **({"resource_effects": [effect.model_dump(mode="json")
                                      for effect in tool.resource_effects]}
               if tool.resource_effects is not None else {}),
        }
        if isinstance(result.metadata, Mapping):
            # Execution state comes only from the MCP result metadata channel,
            # never from output DATA such as a diagnostic object.
            uncertain = result.metadata.get(PROVIDER_RESULT_EXECUTION_UNCERTAIN)
            if isinstance(uncertain, bool):
                provider_metadata[EXECUTION_UNCERTAIN_METADATA_KEY] = uncertain
            terminated = result.metadata.get(PROVIDER_RESULT_EXECUTION_TERMINATED)
            if self._trust_execution_termination and isinstance(terminated, bool):
                provider_metadata[EXECUTION_TERMINATED_METADATA_KEY] = terminated
        return MCPCallResult(
            success=not result.is_error,
            output=result.output,
            error=error,
            started_at=started_at,
            finished_at=finished_at,
            metadata=provider_metadata,
        )

    async def health_check(self) -> ToolHealthStatus:
        healthy = await self._transport.health_check()
        return ToolHealthStatus.HEALTHY if healthy else ToolHealthStatus.UNHEALTHY


def apply_provider_metadata(
    descriptor: ToolDescriptor, metadata: Mapping[str, Any] | None
) -> None:
    # Provider-declared target semantics and command fields from the
    # shared metadata keys. Absent keys keep the contract defaults;
    # no per-tool central list lives here.
    if not metadata:
        return
    annotations = metadata.get("annotations")
    if (isinstance(annotations, Mapping)
            and annotations.get("readOnlyHint") is True):
        descriptor.resource_effects = []
    if PROVIDER_METADATA_RESOURCE_EFFECTS in metadata:
        from redteam.capability.settings import MCPConfigurationError
        try:
            descriptor.resource_effects = TypeAdapter(list[ToolResourceEffect]).validate_python(
                metadata[PROVIDER_METADATA_RESOURCE_EFFECTS], strict=True,
            )
        except ValidationError:
            raise MCPConfigurationError("invalid provider resource effect metadata") from None
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


# Keep the original internal name available to existing callers and tests.
_apply_provider_metadata = apply_provider_metadata


def _safe_error_message(output: dict[str, Any] | str | None) -> str:
    if isinstance(output, str):
        return output[:500]
    if output is None:
        return "MCP tool reported an error"
    diagnostic = output.get("diagnostic")
    if isinstance(diagnostic, dict):
        parts = [value for key in ("code", "message", "repair_condition")
                 if isinstance(value := diagnostic.get(key), str) and value]
        if parts:
            return redact_sensitive_text(": ".join(value[:500] for value in parts))[:500]
    return "MCP tool reported an error; structured details are available in output"


# Backward-compatible name for code written before stdio transport support.
StreamableHTTPMCPAdapter = MCPServerAdapter
