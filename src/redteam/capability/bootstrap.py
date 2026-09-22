"""Capability-owned construction helper used by the application composition root."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from redteam.capability.adapters.mcp.analysis import AnalysisMCPAdapter
from redteam.capability.adapters.mcp.base import MCPAdapter
from redteam.capability.adapters.mcp.example import ExampleMCPAdapter
from redteam.capability.adapters.mcp.mobile import MobileMCPAdapter
from redteam.capability.adapters.mcp.recon import ReconMCPAdapter
from redteam.capability.adapters.mcp.transport import (
    AllowlistedMCPTransport,
    MCPTransport,
    StdioMCPTransport,
    StreamableHTTPMCPTransport,
)
from redteam.capability.adapters.mcp.web_api import WebAPIMCPAdapter
from redteam.capability.gateway import MCPToolGateway
from redteam.capability.registry import MCPToolRegistry
from redteam.capability.settings import (
    MCPConfigurationError,
    MCPServerKind,
    MCPServerSettings,
    MCPTransportKind,
)


async def build_mcp_tool_gateway(server_ids: list[str]) -> MCPToolGateway:
    """Discover configured servers and return a ready-to-invoke gateway.

    An empty list intentionally preserves the deterministic in-process example
    used by the repository's minimal vertical slice.
    """

    return await _build_gateway(server_ids)


@asynccontextmanager
async def open_mcp_tool_gateway(server_ids: list[str]) -> AsyncIterator[MCPToolGateway]:
    """Engagement-scoped connections, required for stateful browser operations.

    The orchestration owner must enter/exit in the same task and finish all
    invocation tasks before exiting. Never share this scope across engagements.
    Partial startup failures close previously opened connections as well.
    """
    async with AsyncExitStack() as stack:
        yield await _build_gateway(server_ids, stack)


async def _build_gateway(
    server_ids: list[str], stack: AsyncExitStack | None = None,
) -> MCPToolGateway:
    if not server_ids:
        example = ExampleMCPAdapter()
        registry = MCPToolRegistry()
        registry.replace_server_tools(example.server_id, await example.discover_tools())
        return MCPToolGateway(
            registry, {example.server_id: example},
            {str(tool.tool_id): ("service_discovery",) for tool in registry.all()},
        )

    if len(server_ids) != len(set(server_ids)):
        raise MCPConfigurationError("CapabilityConfig.mcp_servers contains duplicates")

    registry = MCPToolRegistry()
    adapters: dict[str, MCPAdapter] = {}
    operation_bindings: dict[str, tuple[str, ...]] = {}
    server_concurrency: dict[str, int] = {}
    server_output_limits: dict[str, int] = {}
    for server_id in server_ids:
        settings = MCPServerSettings.from_environment(server_id)
        transport = _create_transport(settings)
        if stack is not None:
            await stack.enter_async_context(transport.session())
        adapter = _create_adapter(settings, transport)
        descriptors = await adapter.discover_tools()
        if not descriptors:
            raise MCPConfigurationError(f"MCP server {server_id!r} discovered no tools")
        if settings.tool_operations is not None:
            discovered_names = {descriptor.tool_name for descriptor in descriptors}
            configured_names = set(settings.tool_operations)
            if discovered_names != configured_names:
                raise MCPConfigurationError(
                    f"MCP server {server_id!r} operation bindings do not match discovered tools"
                )
        for descriptor in descriptors:
            _validate_discovered_schema(descriptor.tool_name, descriptor.input_schema)
            if descriptor.output_schema:
                _validate_discovered_schema(descriptor.tool_name, descriptor.output_schema)
            operations = (
                settings.tool_operations[descriptor.tool_name]
                if settings.tool_operations is not None
                else (descriptor.tool_name,)
            )
            # The operation binding is part of the Registry snapshot exposed
            # to Agent/Orchestration; it must not remain only in a private
            # Gateway map.
            descriptor.operations = list(operations)
            descriptor.target_types = _default_target_types(settings.kind, descriptor.tool_name)
            operation_bindings[str(descriptor.tool_id)] = operations
        registry.replace_server_tools(server_id, descriptors)
        adapters[server_id] = adapter
        server_concurrency[server_id] = settings.max_concurrency
        server_output_limits[server_id] = settings.max_output_bytes

    return MCPToolGateway(
        registry,
        adapters,
        operation_bindings,
        server_concurrency=server_concurrency,
        server_output_limits=server_output_limits,
    )


def _default_target_types(kind: MCPServerKind, tool_name: str) -> list[str]:
    """Conservative target hints for discovery metadata.

    Server-specific policy may narrow these later; these defaults never add
    an execution target and are used only to filter Agent candidates.
    """
    if kind == MCPServerKind.MOBILE:
        return ["device", "asset"]
    if kind == MCPServerKind.ANALYSIS:
        return ["asset", "endpoint", "observation"]
    if kind == MCPServerKind.WEB_API:
        return ["host", "service", "endpoint"]
    if kind == MCPServerKind.RECON:
        return ["host", "service", "endpoint"]
    return []


def _validate_discovered_schema(tool_name: str, schema: dict[str, object]) -> None:
    """Fail closed when an MCP advertises malformed JSON Schema."""
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        raise MCPConfigurationError(
            f"MCP tool {tool_name!r} advertised an invalid JSON schema"
        ) from exc


def _create_adapter(settings: MCPServerSettings, transport: MCPTransport) -> MCPAdapter:
    if settings.allowed_tools is not None:
        transport = AllowlistedMCPTransport(transport, settings.allowed_tools)
    if settings.kind == MCPServerKind.WEB_API:
        return WebAPIMCPAdapter(
            server_id=settings.server_id,
            risk_level=settings.risk_level,
            transport=transport,
        )
    if settings.kind == MCPServerKind.MOBILE:
        return MobileMCPAdapter(
            server_id=settings.server_id,
            risk_level=settings.risk_level,
            transport=transport,
        )
    if settings.kind == MCPServerKind.RECON:
        return ReconMCPAdapter(
            server_id=settings.server_id,
            risk_level=settings.risk_level,
            transport=transport,
        )
    if settings.kind == MCPServerKind.ANALYSIS:
        return AnalysisMCPAdapter(
            server_id=settings.server_id,
            risk_level=settings.risk_level,
            transport=transport,
        )
    raise MCPConfigurationError(f"unsupported MCP server kind: {settings.kind}")


def _create_transport(
    settings: MCPServerSettings,
) -> StreamableHTTPMCPTransport | StdioMCPTransport:
    if settings.transport == MCPTransportKind.STREAMABLE_HTTP:
        if settings.endpoint is None:
            raise MCPConfigurationError("Streamable HTTP MCP endpoint is missing")
        return StreamableHTTPMCPTransport(
            settings.endpoint,
            headers=settings.request_headers(),
        )
    if settings.transport == MCPTransportKind.STDIO:
        if settings.command is None:
            raise MCPConfigurationError("stdio MCP command is missing")
        return StdioMCPTransport(
            settings.command,
            args=settings.args,
            env=settings.process_environment,
        )
    raise MCPConfigurationError(f"unsupported MCP transport: {settings.transport}")
