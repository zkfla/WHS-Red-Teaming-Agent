# Execution Flow

## LangGraph graph

```text
START -> OBSERVE -> PLAN -> POLICY --ALLOW--> EXECUTE -> VERIFY -> UPDATE -> REPLAN -> END
                              \--deny/require_approval--> REPLAN ---^
```

Implementation: `src/redteam/orchestration/graph.py` (`build_graph`),
routing: `src/redteam/orchestration/router.py`.

## Minimal Vertical Slice, step by step

Maps to instruction.md section 16. Every step below is real code, not
a description — run `python -m redteam.app.cli run examples/engagement.yaml`
to see it happen.

| # | Step | Code |
|---|---|---|
| 1 | Engagement created | `app.bootstrap.load_engagement` reads `examples/engagement.yaml` into an `Engagement` |
| 2 | Fake Recon Agent runs | `orchestration.nodes.observe` -> `orchestration.nodes.plan` |
| 3 | Agent creates `ActionProposal` | `orchestration.nodes.plan.plan_node` |
| 4 | Control Plane evaluates | `orchestration.nodes.policy.policy_node` calls `deps.control_plane.evaluate(...)` |
| 5 | ALLOW decision | `control.service.DefaultControlPlane.evaluate` (Scope -> Policy -> RateLimit -> Budget -> Approval) |
| 6 | ToolGateway called | `orchestration.nodes.execute.execute_node` calls `deps.tool_gateway.invoke(...)` — only reachable on ALLOW |
| 7 | Dummy MCP Adapter runs | `capability.gateway.MCPToolGateway` routes to `capability.adapters.mcp.example.ExampleMCPAdapter` |
| 8 | `ToolResult` returned | Normalized shape, regardless of adapter |
| 9 | Evidence created | `orchestration.nodes.verify.verify_node` writes an `EvidenceRecord` via `deps.evidence_store` |
| 10 | Events published | `EventEnvelope`s at every step via `deps.event_bus` — see the full list below |
| 11 | Attack Graph updated | `orchestration.nodes.update.update_node` adds a `GraphNode` via `deps.graph_store` |
| 12 | LangGraph advances state | `RuntimeState.phase` transitions OBSERVE -> ... -> FINISHED |
| 13 | Run ends | `orchestration.nodes.replan.replan_node` sets phase FINISHED; `app.bootstrap.run_engagement` publishes `RunFinished` |

## Events published, in order (happy path)

```text
RunStarted
AgentStarted
ActionProposed
PolicyEvaluated
ActionApproved
ToolExecutionStarted
ToolExecutionFinished
EvidenceCreated
GraphUpdated
RunFinished
```

Verified by `tests/integration/test_end_to_end.py`. `HypothesisCreated`
and `FindingVerified` are not emitted yet — the fake planner doesn't
create Hypotheses/Findings (TODO for Agent Team, see
`orchestration/nodes/plan.py`).

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

The current stub (`orchestration.nodes.verify.verify_node`) only does
the placeholder mapping `success -> PASS`, `not success -> UNKNOWN` —
it does **not** implement real hypothesis verification (there's no
hypothesis logic yet at all). Treat `last_verdict` as meaningless until
Agent Team replaces that TODO; don't read anything into `PASS` beyond
"the tool call didn't fail." No new contract (e.g. a
`VerificationResult`) is planned for this — real verification logic
reuses `Verdict` when it lands.

## Denied path

If `PolicyDecision.verdict` is `DENY` or `REQUIRE_APPROVAL`,
`route_after_policy` sends the graph straight to `REPLAN` — `execute_node`
is never called and `ActionDenied` (not `ActionApproved`) is published
instead.
