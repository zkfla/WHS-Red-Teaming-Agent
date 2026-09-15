"""Capability-owned construction helper used by the application composition root."""

from __future__ import annotations

from redteam.capability.adapters.mcp.analysis import AnalysisMCPAdapter
from redteam.capability.adapters.mcp.base import MCPAdapter
from redteam.capability.adapters.mcp.example import ExampleMCPAdapter
from redteam.capability.adapters.mcp.mobile import MobileMCPAdapter
from redteam.capability.adapters.mcp.recon import ReconMCPAdapter
from redteam.capability.adapters.mcp.transport import (
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

    if not server_ids:
        example = ExampleMCPAdapter()
        registry = MCPToolRegistry()
        registry.replace_server_tools(example.server_id, await example.discover_tools())
        return MCPToolGateway(registry, {example.server_id: example})

    if len(server_ids) != len(set(server_ids)):
        raise MCPConfigurationError("CapabilityConfig.mcp_servers contains duplicates")

    registry = MCPToolRegistry()
    adapters: dict[str, MCPAdapter] = {}
    for server_id in server_ids:
        settings = MCPServerSettings.from_environment(server_id)
        adapter = _create_adapter(settings)
        descriptors = await adapter.discover_tools()
        if not descriptors:
            raise MCPConfigurationError(f"MCP server {server_id!r} discovered no tools")
        registry.replace_server_tools(server_id, descriptors)
        adapters[server_id] = adapter

    return MCPToolGateway(registry, adapters)


def _create_adapter(settings: MCPServerSettings) -> MCPAdapter:
    transport = _create_transport(settings)
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


def _create_transport(settings: MCPServerSettings) -> MCPTransport:
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
