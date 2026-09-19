# Contract Change Proposal: Action-aware Tool Discovery

## Status

Proposed by Capability Team. PM review required before any `core` file changes.

## Problem

`ActionProposal.operation` is a canonical intent and is deliberately separate
from the selected MCP `ToolId`. Orchestration currently has access only to
`ToolGateway.invoke()` and therefore hardcodes a preferred tool binding; it
cannot query the Capability Registry through an approved Core Port.

Importing `redteam.capability` from Orchestration would violate the repository's
dependency rules. Guessing a ToolId from `<capability>.<operation>` would also
violate the Action Naming Convention and could select an execution mechanism
that was not bound to the approved intent.

## Proposed frozen-contract change

Add one synchronous, read-only method to
`src/redteam/core/ports/tool_gateway.py`:

```python
class ToolGateway(Protocol):
    def list_tools_for_action(
        self,
        capability: str,
        operation: str,
    ) -> list[ToolDescriptor]: ...

    async def invoke(self, invocation: ToolInvocation) -> ToolResult: ...
```

The method returns the current Registry snapshot only. It performs no remote
network call, does not execute a Tool, does not make a policy decision, and
does not choose a candidate.

## Security invariants

- Match the exact ToolId namespace to `ActionProposal.capability`.
- Match only operator-configured ToolId-to-operation bindings.
- Exclude tools whose last discovered health is `UNHEALTHY`.
- Return deterministic candidates and no hidden default Tool.
- Keep the final ToolId choice after an ALLOW decision.
- Revalidate the same binding inside `invoke()` to prevent TOCTOU/tool
  substitution even if Orchestration passes a forged ToolId.

Capability already implements and tests these semantics in
`redteam.capability.catalog.MCPToolCatalog`; the Core change only exposes the
read-only query through the existing dependency boundary.

## Impact

- PM/Core: approve and add the Protocol method.
- Capability: concrete implementation is ready.
- Agent/Orchestration: replace the preferred static binding with candidate
  lookup and an explicit selection strategy; handle an empty candidate list
  without calling `invoke()`.
- Tests/fakes: add a deterministic `list_tools_for_action()` implementation.
- Control: no change; ActionProposal is evaluated before execution as today.

## Compatibility and migration

This is additive for callers but structurally breaking for every class that
claims to implement `ToolGateway`. In this repository the known implementations
are `MCPToolGateway` and `DummyToolGateway`. Update the fake in the same
PM-approved contract migration commit, then migrate Orchestration separately.

No ActionProposal, ToolInvocation, ToolDescriptor, or ToolResult field changes
are requested.
