"""Mobile capability adapter for an existing MCP server."""

from __future__ import annotations

from collections.abc import Mapping

from redteam.capability.adapters.mcp.streamable_http import MCPServerAdapter
from redteam.capability.adapters.mcp.transport import MCPTransport
from redteam.core.enums import RiskLevel


class MobileMCPAdapter(MCPServerAdapter):
    def __init__(
        self,
        *,
        server_id: str,
        endpoint: str | None = None,
        risk_level: RiskLevel = RiskLevel.LOW,
        headers: Mapping[str, str] | None = None,
        transport: MCPTransport | None = None,
    ) -> None:
        super().__init__(
            server_id=server_id,
            endpoint=endpoint,
            tool_namespace="mobile",
            capability_tags=("mobile",),
            risk_level=risk_level,
            headers=headers,
            transport=transport,
        )
