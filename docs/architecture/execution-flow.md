# Execution Flow

## LangGraph graph

```text
START -> OBSERVE -> PLAN -> POLICY --ALLOW--> EXECUTE -> VERIFY -> UPDATE -> REPLAN -> END
                              \--deny/require_approval--> REPLAN ---^
```

Implementation: `src/redteam/orchestration/graph.py` (`build_graph`),
routing: `src/redteam/orchestration/router.py`.

## Minimal Vertical Slice, step by step

Maps to instruction.md section 16 and still holds structurally, but two
cells are historical (from when this was the *only* path) — see the
"Current status" note under each. Every step below is real code, not a
description — run `redteam run examples/engagement.yaml` (or bare
`redteam` for the interactive setup) to see it happen.

| # | Step | Code |
|---|---|---|
| 1 | Engagement created | `app.bootstrap.load_engagement` (or, via the CLI, `AppConfig`/`core.config.load_engagement_config`) reads an `engagement.yaml` into an `Engagement` |
| 2 | An Agent runs | `orchestration.nodes.observe` -> `orchestration.nodes.plan`. **Current status**: real registered agents (`recon-agent`/`web-api-agent`/`mobile-agent`, `orchestration/agents/{registry,builtin}.py`), not the single "fake Recon Agent" this row originally described — see `docs/agent/orchestration-implementation-plan.md` section 16.1 for exactly how thin each one's playbook still is. |
| 3 | Agent creates `ActionProposal` | `orchestration.nodes.plan.plan_node` |
| 4 | Control Plane evaluates | `orchestration.nodes.policy.policy_node` calls `deps.control_plane.evaluate(...)` |
| 5 | ALLOW decision | `control.service.DefaultControlPlane.evaluate` (Scope -> Policy -> RateLimit -> Budget -> Approval) |
| 6 | ToolGateway called | `orchestration.nodes.execute.execute_node` calls `deps.tool_gateway.invoke(...)` — only reachable on ALLOW |
| 7 | MCP Adapter runs | `capability.gateway.MCPToolGateway` routes to whichever adapter is configured. **Current status**: `capability.adapters.mcp.example.ExampleMCPAdapter` is still the zero-config default (`capability.mcp_servers: []`), but real adapters exist and run in production configs — see `deploy/mcp/README.md`. |
| 8 | `ToolResult` returned | Normalized shape, regardless of adapter |
| 9 | Evidence created | `orchestration.nodes.verify.verify_node` writes an `EvidenceRecord` via `deps.evidence_store` |
| 10 | Events published | `EventEnvelope`s at every step via `deps.event_bus` — see the full list below |
| 11 | Attack Graph updated | `orchestration.nodes.update.update_node` adds/merges a `GraphNode` (and, for a tool_id with a registered `ResultMapper`, e.g. `recon.http_probe`, real SERVICE/ENDPOINT nodes + edges too — `orchestration/observations/`) via `deps.graph_store` |
| 12 | LangGraph advances state | `RuntimeState.phase` transitions OBSERVE -> ... -> FINISHED |
| 13 | Run ends | `orchestration.nodes.replan.replan_node` sets phase FINISHED; `app.bootstrap.run_engagement` publishes `RunFinished` |

## Events published, in order (happy path)

```text
RunStarted
AgentStarted
CandidateGenerated
ActionProposed
PolicyEvaluated
ActionApproved
ToolExecutionStarted
ToolExecutionFinished
EvidenceCreated
HypothesisResolved
FindingCreated / FindingVerified (when evidence-gated violation is confirmed)
GraphUpdated
ReplanTriggered (for a replan decision)
ReportProjected
RunFinished
```

The exact sequence varies by provider, policy result, mapper output, and
whether a hypothesis reaches a confirmed violation. The action chain keeps
its action correlation; `ReplanTriggered` is a run-level decision event.

## `Verdict` vs `ToolResult.success` — two different axes

Step 9's `verify_node` also sets `RuntimeState.last_verdict`. Don't
confuse it with execution success:

| | meaning | lives on |
|---|---|---|
| `ExecutionStatus` / `ToolResult.success` | did the tool call itself execute without error | `ToolResult` |
| `Verdict` (`PASS`/`VIOLATION`/`ANOMALY`/`UNKNOWN`) | what the (successful or failed) execution implies about the security hypothesis | `RuntimeState.last_verdict` |

Example readings:

```text
success=True  + PASS      -> tool ran fine, hypothesis not confirmed / no violation found
success=True  + VIOLATION -> tool ran fine, and it confirmed a security violation
success=False + UNKNOWN   -> execution failed, so no verdict could be reached
```

**Current status**: this is no longer a `success -> PASS` placeholder.
`orchestration.nodes.verify.verify_node` calls `deps.verifier.verify(...)`
(`DeterministicAnalysisVerifier` by default), which evaluates the
step's `Expectation` against the real `ToolResult`
(`orchestration.expectations.verdict_for`) and resolves the Hypothesis
(`open -> confirmed/rejected/inconclusive`) that was recorded before the
action ran. A `Verdict.VIOLATION` does create a real, verified `Finding`
carrying the `EvidenceId` behind it — this whole mechanism works
correctly end to end (`tests/unit/test_orchestration_nodes.py`).

The actual gap is one level up, not here: reaching `VIOLATION` requires
the step's `Expectation` to set `violation_if_met=True` (or
`anomaly_if_unmet=True`), and **no built-in playbook step
(`recon-agent`/`web-api-agent`/`mobile-agent`) sets either** — so every
real run today produces `PASS`/`UNKNOWN` only, regardless of what the
target actually does. See
`docs/agent/orchestration-implementation-plan.md` section 16.1, finding
1, for the full detail; this is the single most significant remaining
gap in the project.

## Denied path

If `PolicyDecision.verdict` is `DENY` or `REQUIRE_APPROVAL`,
`route_after_policy` sends the graph straight to `REPLAN` — `execute_node`
is never called and `ActionDenied` (not `ActionApproved`) is published
instead.
