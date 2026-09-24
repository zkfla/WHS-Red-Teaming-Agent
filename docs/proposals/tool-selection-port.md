# Contract Change Proposal: Action-aware Tool Discovery

## Status

✅ **Implemented.** `ToolCatalog.list_tools_for_action(capability, operation)`
is live on the Core-owned `redteam.core.ports.tool_catalog.ToolCatalog` port.
`redteam.orchestration.candidates.catalog.ToolCatalog` is only a compatibility
re-export of that Core port, so Agent and Capability cannot drift into two
different catalog contracts. `redteam.orchestration.candidates.tool_selection.select_tool`
is the deterministic priority (healthy > unknown, then lower risk_level,
then stable `tool_id` lexical order — historical failure rate is not a
tier: no per-tool failure signal exists anywhere in this codebase yet, and
this proposal explicitly said not to fake one). Wired into
`CandidateCompiler._resolve_tool`, replacing the old exact-preferred-tool_id
lookup — Orchestration no longer pins a hardcoded `ToolId`; it asks the
Capability Registry through `RuntimeDeps.tool_catalog` which tools are
eligible and picks among them. `MCPToolGateway` implements the read-only
catalog surface, while execution remains on `ToolGateway.invoke()`.

Originally proposed by Capability Team; PM review completed.

## Problem

`ActionProposal.operation` is a canonical intent and is deliberately separate
from the selected MCP `ToolId`. Orchestration must query the Capability
Registry through the approved Core `ToolCatalog` port rather than importing a
MCP implementation or hardcoding a preferred tool binding.

Importing `redteam.capability` from Orchestration would violate the repository's
dependency rules. Guessing a ToolId from `<capability>.<operation>` would also
violate the Action Naming Convention and could select an execution mechanism
that was not bound to the approved intent.

## Proposed frozen-contract change

Add one synchronous, read-only method to
`src/redteam/core/ports/tool_catalog.py`:

```python
class ToolCatalog(Protocol):
    def list_tools_for_action(
        self,
        capability: str,
        operation: str,
    ) -> list[ToolDescriptor]: ...

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
read-only query through the existing dependency boundary. `ToolGateway` stays
execution-only, keeping policy admission and execution separate.

## Impact

- PM/Core: approve and add the Protocol method.
- Capability: concrete implementation is ready.
- Agent/Orchestration: inject `RuntimeDeps.tool_catalog`, replace the preferred
  static binding with candidate lookup and an explicit selection strategy, and
  handle an empty candidate list without calling `invoke()`.
- Tests/fakes: add a deterministic `list_tools_for_action()` implementation.
- Control: no change; ActionProposal is evaluated before execution as today.

## Compatibility and migration

This is additive for callers. Existing `ToolGateway` implementations only need
to support `invoke()`. Implementations wired into `RuntimeDeps.tool_catalog`
must satisfy the new Core `ToolCatalog` port; the orchestration compatibility
module re-exports that single definition.

No ActionProposal, ToolInvocation, ToolDescriptor, or ToolResult field changes
are requested.
