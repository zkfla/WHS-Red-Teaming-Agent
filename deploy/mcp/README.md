# MCP Server deployment notes

Owned by Capability Team.

This stage ships exactly one MCP server: `example-mcp`, a harmless
dummy used to validate the end-to-end flow (see
`redteam.capability.adapters.mcp.example.ExampleMCPAdapter`, which
currently runs in-process and does not actually call this container —
wire it up for real once the container's transport is decided).

## Naming conventions

- Container name: `redteam-mcp-<server-id>` (e.g. `redteam-mcp-example`)
- Network name: `redteam-net`
- Volume policy: MCP server containers should be stateless where
  possible; if a server needs persistent state, mount a named volume
  under `redteam-mcp-<server-id>-data` and document it here.

## Health check

Each MCP server container must expose a way for
`MCPAdapter.health_check()` to determine `ToolHealthStatus` — an HTTP
health endpoint is the default assumption in
`deploy/compose/docker-compose.yml`. Adjust per server as needed.

## Adding a new MCP server

1. Add the server's image/build to `deploy/compose/docker-compose.yml`
   (see the `mcp-example` service for the shape).
2. Implement an `MCPAdapter` for it under
   `src/redteam/capability/adapters/mcp/`.
3. Register its `ToolDescriptor`s via `MCPToolRegistry` in
   `redteam.app.bootstrap.build_runtime_deps`.
4. No other code should need to change — that's the point of the
   Adapter pattern (instruction.md section 11).

Real attack-tool MCP servers are explicitly out of scope for this
stage (instruction.md section 21).
