# Team Boundaries

| 영역 | 담당 | 변경 가능 영역 |
|---|---|---|
| Core Contracts | PM | PM 승인 필요 |
| Architecture | PM | PM 승인 필요 |
| Orchestration | Agent Team | 자유 |
| Attack Graph | Agent Team | 자유 |
| Event Bus | Agent Team | 자유 |
| Evidence Store | Control Team | 자유 |
| Control Plane | Control Team | 자유 |
| Capability | Capability Team | 자유 |
| MCP Adapter | Capability Team | 자유 |
| Execution Substrate | Capability Team | 자유 |

## Team Integration Points

What each team implements, and the `core` contracts each mainly reads —
this is everything a team needs to start; no team should need to define
its own version of any contract listed here. Full contract shapes:
`docs/contracts/`.

```text
Agent Team
  구현 대상:
    EventBus
    AttackGraphStore
    LangGraph Orchestration (orchestration.nodes.*)
  주요 사용 Contract:
    RuntimeState
    ActionProposal
    EventEnvelope
    GraphNode
    GraphEdge
    Verdict

Control Team
  구현 대상:
    ControlPlane
    EvidenceStore
  주요 사용 Contract:
    Engagement
    ControlContext
    ActionProposal
    PolicyDecision
    EvidenceRecord

Capability Team
  구현 대상:
    ToolGateway
    MCPAdapter
    MCP Registry
    Execution Substrate
  주요 사용 Contract:
    ToolInvocation
    ToolResult
    ToolDescriptor
    ToolHealthStatus
```

Each team reaches the others only through `core/ports` +
`core/contracts` — never another team's concrete module directly (see
"Dependency Rules" below and `tests/unit/test_control_plane_bypass.py`,
which checks this structurally for Orchestration).

## Contract v0.1 / Schema Change Process

`core/ids.py`, `core/enums.py`, `core/contracts/**`, `core/ports/**` are
frozen as **Contract v0.1** — see `docs/contracts/versioning.md` for
what that means and the full change procedure. Summary: any change to
`src/redteam/core/contracts/*`, `src/redteam/core/ports/*`,
`src/redteam/core/ids.py`, or `src/redteam/core/enums.py` — i.e.
anything another team's code imports — follows:

```text
Issue
  -> Schema Change Proposal (what field/type/Protocol changes, why, who's affected)
  -> PM Review
  -> Merge
```

Do not change a `core` file directly in a PR that also contains your
team's feature work — split it into its own PR so PM can review the
contract change in isolation.

## What "자유" (free) means in practice

Within your own team's modules (e.g. Agent Team across `orchestration`
+ `attack_graph` + `event_bus`), you can refactor, add files, change
internal function signatures, etc. without PM review — as long as:

1. You don't change what you import from `core`.
2. You don't start importing another team's concrete module (see
   `dependency-rules.md` — this is enforced by
   `tests/unit/test_control_plane_bypass.py`, not just convention).
3. Tests still pass — a red `tests/contract` or `tests/integration`
   suite blocks merge for everyone, since those exercise the shared
   vertical slice.
