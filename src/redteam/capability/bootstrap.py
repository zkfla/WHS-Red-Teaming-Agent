"""Capability-owned construction helper used by the application composition root."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager

from redteam.capability.adapters.mcp.analysis import AnalysisMCPAdapter
from redteam.capability.adapters.mcp.base import MCPAdapter
from redteam.capability.adapters.mcp.example import ExampleMCPAdapter
from redteam.capability.adapters.mcp.mobile import MobileMCPAdapter
from redteam.capability.adapters.mcp.recon import ReconMCPAdapter
from redteam.capability.adapters.mcp.streamable_http import MCPServerAdapter
from redteam.capability.adapters.mcp.transport import (
    AllowlistedMCPTransport,
    MCPTransport,
    StdioMCPTransport,
    StreamableHTTPMCPTransport,
)
from redteam.capability.adapters.mcp.web_api import WebAPIMCPAdapter
from redteam.capability.gateway import MCPToolGateway
from redteam.capability.mobile_analysis_manifest import (
    MOBILE_ANALYSIS_TOOLS,
    validate_configured_discovery,
)
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
    output_schemas: dict[str, dict | None] = {}
    target_types: dict[str, str | None] = {}
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
        server_bindings: dict[str, tuple[str, ...]] = {}
        for descriptor in descriptors:
            operations = (
                settings.tool_operations[descriptor.tool_name]
                if settings.tool_operations is not None
                else (descriptor.tool_name,)
            )
            operation_bindings[str(descriptor.tool_id)] = operations
            server_bindings[str(descriptor.tool_id)] = operations
            if isinstance(adapter, MCPServerAdapter):
                schema = adapter.output_schema_for_tool(descriptor.tool_id)
                if schema is not None:
                    output_schemas[str(descriptor.tool_id)] = schema
        if settings.tool_operations is not None and server_id in MOBILE_ANALYSIS_TOOLS:
            server_schemas = {
                descriptor.tool_name: output_schemas.get(str(descriptor.tool_id))
                for descriptor in descriptors
            }
            try:
                validate_configured_discovery(
                    server_id, descriptors, server_bindings, server_schemas
                )
            except ValueError as exc:
                raise MCPConfigurationError(
                    f"MCP server {server_id!r} discovery differs from configured manifest: {exc}"
                ) from exc
        for spec in MOBILE_ANALYSIS_TOOLS.get(server_id, ()):
            if spec.tool_id in server_bindings:
                target_types[spec.tool_id] = spec.target_type
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
        output_schemas=output_schemas,
        target_types=target_types,
    )


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
