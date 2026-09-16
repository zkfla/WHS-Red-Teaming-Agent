# ADR 0003: MCP-Based Capability Layer, Adapter Pattern

## Status

Accepted.

## Context

Security capabilities (recon tools, scanners, etc.) should come from
existing MCP servers, not be built from scratch inside this framework
(``새 보안 Tool을 직접 만들지 않는다``).
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
- `ActionProposal.operation` is the tool-independent intent evaluated by
  Control. `ToolInvocation.tool_id` selects the execution mechanism after
  ALLOW. The Gateway resolves the MCP remote tool name from the registered
  `ToolDescriptor`; it never requires the intent name to equal that remote
  tool name.

The example adapter remains as a harmless in-process test double. Real MCP
servers use stdio, Streamable HTTP, SSE, or legacy HTTP transports through
the same adapter boundary. Product-specific tools are allowlisted before
they enter the Registry.

## Consequences

- Capability Team can build/select real MCP server adapters without
  reading Agent/Orchestration code — they only implement `MCPAdapter`
  and register `ToolDescriptor`s in
  `redteam.app.bootstrap.build_runtime_deps`.
- Swapping or adding an MCP server never touches
  `redteam.orchestration` — verified structurally by
  `tests/unit/test_control_plane_bypass.py` (orchestration cannot
  import `redteam.capability` at all, only `redteam.core.ports.tool_gateway`).
- The execution-time ToolId must be chosen independently from the canonical
  action key. One action can have multiple eligible tools, and a ToolId may
  happen to look identical to an action key without becoming the same
  contract value.
- Configured servers bind every allowlisted remote tool to zero or more
  canonical operations. The Gateway rejects a ToolId selected outside that
  binding, preventing a caller from obtaining approval for one intent and
  substituting a different execution mechanism. An empty binding reserves a
  tool for operator-only lifecycle work.
- `MCPToolCatalog` returns healthy, namespace-matched candidates for a
  canonical action but deliberately does not choose one. Exposing that query
  to Orchestration requires the frozen Core Port change described in
  `docs/proposals/tool-selection-port.md`.
- The Gateway applies a per-server concurrency semaphore and includes queue
  wait time in the invocation timeout. A tool discovered from an unhealthy
  server is rejected before transport invocation; there is still no automatic
  retry because tool calls cannot be assumed idempotent.
- `deploy/mcp/finance` provides local Web/API and Recon MCP services plus a
  persistent browser connection example for the synthetic financial lab.
