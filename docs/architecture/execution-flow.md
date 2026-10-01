# Execution Flow

## LangGraph graph

```text
START -> OBSERVE -> PLAN -> POLICY --ALLOW--> EXECUTE -> VERIFY -> UPDATE -> REPLAN -> END
                              \--deny/require_approval--> REPLAN ---^
```

Implementation: `src/redteam/orchestration/graph.py` (`build_graph`),
routing: `src/redteam/orchestration/router.py`.

Control/tool/state outcomes are also appended to the bounded
`RuntimeState.recent_observations` window and supplied to the next planning
turn. The CLI displays the actual decision, Control verdict, tool result and
state update; approval suspension preserves the original action for resume.

The interactive shell classifies chat before mutation: read-only questions,
guidance for the original Run attachment, explicit new investigations, or
clarification. A session-wide editor preserves drafts across Run completion;
control commands and chat classification use separate workers. See
[CLI session architecture](cli-session.md) for routing, output fences and lifecycle.

The shell sends accepted execution requests into a Mission objective/guidance, but the
visible action response comes from the PLAN node's actual LLM turn. The facade
has no ToolGateway path. Therefore natural language cannot skip PLAN, the
candidate compiler, POLICY or MCP selection.

PLAN receives the current Attack Graph, Evidence excerpts, prior Control/tool
observations (the bounded 12-record runtime window), operator guidance and the
healthy live MCP action space. It returns a public decision summary (public
reason only, never private chain-of-thought) plus structured action drafts
through `path_draft` -> `path_review` -> `next_action`. Compiled drafts that
sit inside the accepted path version continue to Control; a failed model call
or a draft that fails compilation returns errors for model correction and
never executes a rule/playbook fallback. Completion modes (`complete_step`,
`complete_task`, `complete_goal`) close work only through code validation
against real evidence.

## Minimal Vertical Slice, step by step

Maps to instruction.md section 16 and still holds structurally, but two
cells are historical (from when this was the *only* path) — see the
"Current status" note under each. Every step below is real code, not a
description — run `redteam run examples/engagement.yaml` (or bare
`redteam` for the interactive setup) to see it happen.

| # | Step | Code |
|---|---|---|
| 1 | Engagement created | `app.bootstrap.load_engagement` (or, via the CLI, `AppConfig`/`core.config.load_engagement_config`) reads an `engagement.yaml` into an `Engagement` |
| 2 | An Agent runs | `orchestration.nodes.observe` -> `orchestration.nodes.plan`. **Current status**: real registered agents (`recon-agent`/`web-api-agent`/`mobile-agent`, `orchestration/agents/{registry,builtin}.py`), not the single "fake Recon Agent" this row originally described; role providers supply review reference only and every executed action comes from the accepted compiled path. |
| 3 | Agent creates `ActionProposal` | `orchestration.nodes.plan.plan_node` |
| 4 | Control Plane evaluates | `orchestration.nodes.policy.policy_node` calls `deps.control_plane.evaluate(...)` |
| 5 | ALLOW decision | `control.service.DefaultControlPlane.evaluate` (Scope -> Policy -> RateLimit -> Budget -> Approval) |
| 6 | ToolGateway called | `orchestration.nodes.execute.execute_node` calls `deps.tool_gateway.invoke(...)` — only reachable on ALLOW |
| 7 | MCP Adapter runs | `capability.gateway.MCPToolGateway` routes to whichever adapter is configured. **Current status**: an empty/unset `capability.mcp_servers` (`REDTEAM_MCP_SERVERS` unset, no CLI config file at all) now gets an honest empty tool catalog (`app.bootstrap._empty_tool_gateway`) — no Action can execute, but nothing pretends a fake tool ran. `capability.adapters.mcp.example.ExampleMCPAdapter` only appears via the explicit, literal opt-in `REDTEAM_MCP_SERVERS=example`; real adapters exist and run in production configs — see `deploy/mcp/README.md`. |
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
AgentContextPrepared (path draft)
AgentTurnDecided (path draft)
AgentContextPrepared (path review)
AgentTurnDecided (path review)
AgentContextPrepared (next action)
AgentTurnDecided (next action)
AgentDecisionRecorded
MCPToolSelected
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

`orchestration.nodes.verify.verify_node` uses the structured LLM analysis
verifier by default. It interprets the real result against the action's
hypothesis and expectation, while code checks the referenced run, invocation,
target and principal evidence. A verified violation can create a Finding
linked to the supporting evidence. A successful tool call alone does not
establish a vulnerability or complete the path, task or goal. The retained
deterministic verifier supports explicit test scenarios; it does not replace
the production LLM planning flow.

## Denied path

If `PolicyDecision.verdict` is `DENY` or `REQUIRE_APPROVAL`,
`route_after_policy` sends the graph straight to `REPLAN` — `execute_node`
is never called and `ActionDenied` (not `ActionApproved`) is published
instead.

Before replanning, that exact verdict, reason, matched policy rules and
approval id are converted into a bounded `RuntimeObservation`. The next LLM
turn therefore sees “outside allowed paths” or “approval required”, not a
generic tool failure.
