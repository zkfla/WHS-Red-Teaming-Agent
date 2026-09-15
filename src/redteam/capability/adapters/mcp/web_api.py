"""Web/API capability adapter for an existing remote MCP server."""

from __future__ import annotations

from redteam.capability.adapters.mcp.streamable_http import StreamableHTTPMCPAdapter
from redteam.capability.adapters.mcp.transport import MCPTransport


class WebAPIMCPAdapter(StreamableHTTPMCPAdapter):
    def __init__(
        self,
        *,
        server_id: str,
        endpoint: str,
        transport: MCPTransport | None = None,
    ) -> None:
        super().__init__(
            server_id=server_id,
            endpoint=endpoint,
            tool_namespace="web",
            capability_tags=("web", "api"),
            transport=transport,
        )
