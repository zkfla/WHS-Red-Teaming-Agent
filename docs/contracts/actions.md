# Actions & Policy Decisions

Schemas: `redteam.core.contracts.action`, `redteam.core.contracts.target`,
`redteam.core.contracts.policy`. Conversion helper:
`redteam.core.convert.build_tool_invocation`.

## The rule

Agents never call the Capability Layer directly. They build an
`ActionProposal` and hand it, with a `ControlContext`, to the Control
Plane (`redteam.core.ports.control_plane.ControlPlane`). Only an ALLOW
`PolicyDecision` may become a `ToolInvocation` —
`redteam.core.convert.build_tool_invocation` enforces this mechanically
(raises `NonAllowDecisionError` otherwise).

## ActionProposal

```python
class ActionProposal(BaseModel):
    action_id: ActionId
    run_id: RunId
    engagement_id: EngagementId
    agent_id: AgentId
    capability: str
    operation: str
    target: TargetRef
    arguments: dict[str, Any] = {}   # deliberately open — every MCP tool defines its own schema
    reason: str
    hypothesis_id: HypothesisId | None = None
    expected_observation: str | None = None
    created_at: datetime
```

## TargetRef — discriminated union, not a flat dict

`redteam.core.contracts.target.TargetRef` is a `kind`-discriminated
union with one variant per target shape:

```text
HostTarget       kind="host"        host
DomainTarget     kind="domain"      domain
UrlTarget        kind="url"         url
EndpointTarget   kind="endpoint"    host, port?, path?
ServiceTarget    kind="service"     service_name, host?
PrincipalTarget  kind="principal"   principal_id
AssetTarget      kind="asset"       asset_id
GraphNodeTarget  kind="graph_node"  node_id, node_type?   (escape hatch)
```

Every variant except `PrincipalTarget`/`AssetTarget` carries an optional
`graph_node_id` once the target has been linked into the Shared Attack
Graph. Use the accessor helpers instead of `isinstance`-matching by hand
where possible:

```python
from redteam.core.contracts.target import target_graph_node_id, target_display_name

target_graph_node_id(proposal.target)  # -> GraphNodeId | None
target_display_name(proposal.target)   # -> str, human-readable only
```

## ControlContext

```python
class ControlContext(BaseModel):
    engagement_id: EngagementId
    agent_id: AgentId | None = None
    correlation_id: str | None = None
    metadata: dict[str, Any] = {}
```

## PolicyDecision

```python
class PolicyDecision(BaseModel):
    action_id: ActionId
    verdict: PolicyVerdict  # ALLOW | DENY | REQUIRE_APPROVAL
    reasons: list[str] = []
    matched_rules: list[str] = []
    approval_id: ApprovalId | None = None
    rate_limit_remaining: int | None = None
    budget_remaining: int | None = None
    evaluated_at: datetime
```

## Control Plane pipeline

```text
ActionProposal
  -> Scope Check       (redteam.control.scope.check_scope)
  -> ROE Check          (pass-through stub — TODO)
  -> Policy Engine      (redteam.control.policy.evaluate_policy)
  -> Rate Limit         (redteam.control.rate_limit.RateLimiter)
  -> Budget             (redteam.control.budget.BudgetTracker)
  -> Approval           (redteam.control.approval.ApprovalService, only if REQUIRE_APPROVAL and not auto_approve)
  -> ALLOW / DENY / REQUIRE_APPROVAL
```

Implementation: `redteam.control.service.DefaultControlPlane.evaluate`.
Every branch returns a `PolicyDecision` — there is no code path that
reaches `execute_node` without one.

## ToolInvocation / ToolResult

Only built from an ALLOW `PolicyDecision`, via
`redteam.core.convert.build_tool_invocation` (used by
`orchestration.nodes.execute.execute_node`):

```python
class ToolInvocation(BaseModel):
    invocation_id: InvocationId
    action_id: ActionId
    run_id: RunId
    tool_id: ToolId
    operation: str
    arguments: dict[str, Any] = {}
    timeout_seconds: float = 30.0   # validated > 0
    execution_context: ExecutionContext
    created_at: datetime

class ToolResult(BaseModel):
    invocation_id: InvocationId
    action_id: ActionId
    tool_id: ToolId
    success: bool
    output: dict[str, Any] | str | None = None
    error: ToolError | None = None
    started_at: datetime
    finished_at: datetime            # validated >= started_at
    metadata: dict[str, Any] = {}
```

Every MCP adapter's output is normalized into this shape by
`redteam.capability.gateway.MCPToolGateway` before anything else in the
framework sees it — including filling in `action_id`/`invocation_id`,
which adapters don't know about (see
`redteam.capability.adapters.mcp.example.ExampleMCPAdapter`).
