"""Placeholder entry point for a real example MCP server.

TODO(capability-team): implement a minimal MCP server here (stdio or
HTTP transport) exposing one harmless "ping" tool, matching the
descriptor currently hardcoded in
``redteam.capability.adapters.mcp.example.ExampleMCPAdapter``. Until
this exists, that adapter simulates the call in-process so the rest of
the framework can be exercised without it.
"""

import http.server
import json
import os


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path == "/health":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps({"status": "healthy"}).encode())
            return
        self.send_response(404)
        self.end_headers()


if __name__ == "__main__":
    port = int(os.environ.get("MCP_EXAMPLE_PORT", "8765"))
    http.server.HTTPServer(("0.0.0.0", port), Handler).serve_forever()
