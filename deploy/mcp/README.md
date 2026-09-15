# MCP Server deployment notes

Owned by Capability Team.

This stage ships exactly one MCP server: `example-mcp`, a harmless
server used to validate discovery and invocation over MCP Streamable
HTTP. It exposes a single `ping` tool at `http://localhost:8765/mcp`.
`ExampleMCPAdapter` remains an in-process test double; real endpoints
use `WebAPIMCPAdapter` or `ReconMCPAdapter`.

## Naming conventions

- Container name: `redteam-mcp-<server-id>` (e.g. `redteam-mcp-example`)
- Network name: `redteam-net`
- Volume policy: MCP server containers should be stateless where
  possible; if a server needs persistent state, mount a named volume
  under `redteam-mcp-<server-id>-data` and document it here.

## Health check

Each MCP Adapter performs a protocol-level connection health check.
The Compose healthcheck separately verifies that the container is
listening so Docker can manage its lifecycle without embedding a
second application-specific health API.

## Tool identity and registration

Remote tools are exposed as deterministic framework IDs:

```text
<capability-namespace>.<normalized-remote-tool-name>
```

Examples: `web.send_http_request`, `recon.probe_http`. Adapter discovery
returns a complete server snapshot and `MCPToolRegistry.replace_server_tools`
atomically replaces only that server's tools. A conflicting `ToolId`
owned by another server is rejected instead of silently overwritten.

## Adding a new MCP server

1. Add the server's image/build to `deploy/compose/docker-compose.yml`
   (see the `mcp-example` service for the shape).
2. Configure a `WebAPIMCPAdapter`, `ReconMCPAdapter`, or implement an
   `MCPAdapter` under `src/redteam/capability/adapters/mcp/`.
3. Discover and register its `ToolDescriptor`s via `MCPToolRegistry` in
   `redteam.app.bootstrap.build_runtime_deps`.
4. No other code should need to change — that's the point of the
   Adapter pattern (instruction.md section 11).

Real attack-tool MCP servers are explicitly out of scope for this
stage (instruction.md section 21).
