"""Harmless MCP server used to verify the real Streamable HTTP path."""

import os

from mcp.server import MCPServer

mcp = MCPServer("redteam-example")


@mcp.tool()
def ping(message: str = "ping") -> dict[str, str]:
    """Echo a harmless message for local Capability integration tests."""

    return {"message": message, "status": "ok"}


if __name__ == "__main__":
    transport = os.environ.get("MCP_EXAMPLE_TRANSPORT", "streamable-http")
    if transport == "stdio":
        mcp.run(transport="stdio")
    else:
        port = int(os.environ.get("MCP_EXAMPLE_PORT", "8765"))
        mcp.run(
            transport="streamable-http",
            host="0.0.0.0",
            port=port,
            stateless_http=True,
            json_response=True,
        )
