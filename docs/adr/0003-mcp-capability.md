# ADR 0003: MCP-Based Capability Layer, Adapter Pattern

## Status

Accepted.

## Context

Security capabilities (recon tools, scanners, etc.) should come from
existing MCP servers, not be built from scratch inside this framework
(instruction.md section 3, 10, 21: "새 보안 Tool을 직접 만들지 않는다").
Different MCP servers will have different transports and tool shapes;
the framework must not need to change when a server is added, removed,
or swapped.

## Decision

```text
Framework -> ToolGateway -> MCPToolRegistry -> MCPAdapter -> Existing MCP Server
```

- `redteam.core.ports.tool_gateway.ToolGateway` — the only interface
  Orchestration depends on (`invoke(ToolInvocation) -> ToolResult`).
- `redteam.capability.gateway.MCPToolGateway` — the concrete
  implementation; the only class allowed to call an `MCPAdapter`
  directly.
- `redteam.capability.registry.MCPToolRegistry` — tracks which
  `ToolDescriptor` lives on which `server_id`.
- `redteam.capability.adapters.mcp.base.MCPAdapter` (Protocol:
  `discover_tools`, `invoke`, `health_check`) — one implementation per
  MCP server. Adding a server means adding one adapter class; no other
  code changes.

At this stage, exactly one adapter exists:
`redteam.capability.adapters.mcp.example.ExampleMCPAdapter`, a harmless
dummy that simulates a call in-process (no real MCP transport yet) so
the end-to-end flow can be exercised without any real security tool
connected (instruction.md section 12/21).

## Consequences

- Capability Team can build/select real MCP server adapters without
  reading Agent/Orchestration code — they only implement `MCPAdapter`
  and register `ToolDescriptor`s in
  `redteam.app.bootstrap.build_runtime_deps`.
- Swapping or adding an MCP server never touches
  `redteam.orchestration` — verified structurally by
  `tests/unit/test_control_plane_bypass.py` (orchestration cannot
  import `redteam.capability` at all, only `redteam.core.ports.tool_gateway`).
- `deploy/mcp` and `deploy/compose/docker-compose.yml` currently ship
  the `mcp-example` placeholder container (not yet wired to a real MCP
  transport — `ExampleMCPAdapter` runs in-process instead). Wiring a
  real transport (stdio/HTTP) is a TODO left for Capability Team once
  the first real MCP server is chosen.
