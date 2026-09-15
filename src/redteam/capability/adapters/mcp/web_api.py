"""Web/API capability adapter for an existing remote MCP server."""

from __future__ import annotations

from collections.abc import Mapping

from redteam.capability.adapters.mcp.streamable_http import StreamableHTTPMCPAdapter
from redteam.capability.adapters.mcp.transport import MCPTransport
from redteam.core.enums import RiskLevel


class WebAPIMCPAdapter(StreamableHTTPMCPAdapter):
    def __init__(
        self,
        *,
        server_id: str,
        endpoint: str,
        risk_level: RiskLevel = RiskLevel.LOW,
        headers: Mapping[str, str] | None = None,
        transport: MCPTransport | None = None,
    ) -> None:
        super().__init__(
            server_id=server_id,
            endpoint=endpoint,
            tool_namespace="web",
            capability_tags=("web", "api"),
            risk_level=risk_level,
            headers=headers,
            transport=transport,
        )
