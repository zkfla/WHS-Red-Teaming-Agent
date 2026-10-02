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
    availability_impact: bool = False
    created_at: datetime
```

`availability_impact` — agent-declared intent, defaults `False` ("no
known impact"): does *this specific action* risk disrupting the target's
availability (flood/exhaustion/restart-style effects), as opposed to a
read-only or low-volume probe? Never inferred from `operation`'s name.
Consumed by `redteam.control.roe.check_roe` against
`RuleOfEngagement.availability_impact_allowed` — see
`redteam.control.roe`'s module docstring for what ROE does and does not
enforce yet (`prohibited_techniques` has no taxonomy anywhere in this
codebase and fails a run closed rather than being silently ignored).

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
DeviceTarget     kind="device"      device_serial, device_ref, package_name, asset_id?
GraphNodeTarget  kind="graph_node"  node_id, node_type?   (escape hatch)
```

`DeviceTarget.device_serial` is the operator-configured adb serial. `device_ref`
is the separate operator-configured identifier accepted by the selected mobile
MCP server; orchestration does not derive one from the other.

Every variant except `PrincipalTarget`/`AssetTarget` carries an optional
`graph_node_id` once the target has been linked into the Shared Attack
Graph. Use the accessor helpers instead of `isinstance`-matching by hand
where possible:

```python
from redteam.core.contracts.target import target_graph_node_id, target_display_name

target_graph_node_id(proposal.target)  # -> GraphNodeId | None
target_display_name(proposal.target)   # -> str, human-readable only
```

## Action Naming Convention

### `ActionProposal.capability`

Canonical namespace, identical to the namespace vocabulary used by the
Capability Layer. v1 allowed values:

```text
recon
web
mobile
analysis
```

Rules:

* lowercase only
* no aliases
* must not embed an operation (`recon.port_scan` is invalid as a
  `capability` value)
* no free-form spelling (`web_api`, `WEB`, `reconnaissance`)

### `ActionProposal.operation`

Canonical, tool-independent action name describing what the agent
intends to do — not which tool implements it.

Format: `lower_snake_case`.

Examples: `port_scan`, `service_discovery`, `endpoint_discovery`,
`http_request`, `auth_attempt`, `parameter_fuzz`, `file_upload`,
`app_launch`, `device_shell`, `runtime_instrumentation`,
`static_analysis`, `response_analysis`.

Workspace-tool extension on the capability branch: `directory_list`,
`file_read`, `file_search`, `file_write`, `file_edit`, under the existing
`analysis` capability with an approved `AssetTarget`. These describe intent;
the corresponding MCP ToolIds are `analysis.list_directory`,
`analysis.read_file`, `analysis.search_files`, `analysis.write_file`,
`analysis.apply_patch`. Read and mutation permissions remain separate.
This is an additive naming extension, not a new capability/target schema
or permission to automatically schedule these actions. PM/Agent/Control
owners should review it before integration into dev; deployment ROE must
explicitly permit the workspace asset and intended operations.

Rules:

* `operation` need not equal any MCP `tool_name`
* must not be tied to a specific product/tool (`nmap_scan`,
  `frida_attach`, `burp_request` are not valid `operation` values)
* the same intent may be fulfilled by multiple Tools, e.g. `recon.port_scan`
  can be executed by `recon.nmap_scan`, `recon.masscan_scan`, or
  `recon.rustscan`

### Canonical Action Key

The Control Plane evaluates ROE operation policy against:

```text
<capability>.<operation>
```

Example: `capability="recon"`, `operation="port_scan"` →
action key `"recon.port_scan"`.

### Matching against ROE

```yaml
denied_capabilities:
  - mobile

denied_operations:
  - recon.port_scan
  - web.file_upload
  - web.auth_attempt
```

* `allowed_capabilities` / `denied_capabilities` — exact match against
  the `capability` namespace.
* `allowed_operations` / `denied_operations` — exact match against the
  `<capability>.<operation>` canonical action key.
* v1 defines no wildcard or namespace-prefix matching as part of the
  naming convention. To block all of `web`, deny the `web` capability;
  to block one behavior, deny `web.file_upload`.

### Action Key vs. ToolId

The Capability Layer's `ToolId` keeps its existing rule:

```text
<namespace>.<remote_tool_name>
```

Examples: `recon.nmap_scan`, `mobile.frida_attach`, `web.http_client`.

`ToolId` and the canonical Action Key can look similar but are
semantically different values:

```text
Action capability / operation  = what the agent intends to do (intent)
ToolId                          = what executes it (execution mechanism)
```

Flow:

```text
ActionProposal
  capability = recon
  operation  = port_scan

        |
        v

ROE evaluation target
  recon.port_scan

        | ALLOW
        v

Tool selection
  recon.nmap_scan
```

`ToolId` is selected during the execution step, after the ROE decision.
No string equality between the Action Key and the selected `ToolId` is
guaranteed.

### Correct examples

```text
capability = "recon"
operation  = "port_scan"

action key = "recon.port_scan"

selected ToolId after ALLOW:
recon.nmap_scan
```

### Incorrect examples

```text
capability = "recon.port_scan"   # operation embedded in capability
capability = "WEB"               # violates canonical namespace casing
operation  = "NmapScan"          # wrong case/format, also tool-specific
operation  = "frida_attach"      # tied to a specific tool implementation
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
  -> ROE Check          (redteam.control.roe.check_roe — real: exact
                          denied/allowed_operations match, `availability_impact_allowed`
                          enforced against the agent-declared `ActionProposal.availability_impact`,
                          `prohibited_techniques` fails the whole run closed
                          with `UnsupportedRoePolicyError` rather than being
                          silently ignored — no technique taxonomy exists yet)
  -> Policy Engine      (redteam.control.policy.evaluate_policy — also
                          risk-tiered: an action whose Tool Registry
                          `risk_level` meets a threshold requires approval
                          regardless of `require_approval_for`, via the
                          read-only `ToolGateway.list_tools_for_action`
                          port; see `redteam.control.risk` and
                          `docs/control/PLAN.md` section 2.1. A Registry
                          lookup failure also requires approval — manual
                          escalation, not a fail-closed DENY, since a
                          human can still allow it)
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

A failed adapter call (`success=False`, a populated `error`) is returned
as-is, before the output-size and output-schema checks below it in
`MCPToolGateway.execute` — those two only ever validate a *successful*
call's `output`. Running them unconditionally used to mask the adapter's
real `error` (never shaped like the tool's success schema, so it always
failed schema validation) behind a generic `tool_output_schema_mismatch`
naming the wrong problem; the server's own `ToolError`/message (e.g. "target
address outside configured networks") reaches `ToolResult.error` now.
