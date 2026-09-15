# Agent Orchestration

Code: `src/redteam/orchestration/`. Contracts used: `RuntimeState`
(ours), `ActionProposal`, `PolicyDecision`, `ToolInvocation`/`ToolResult`,
`EvidenceRecord`, `EventEnvelope`, `Hypothesis`/`Finding`, `Verdict`.

## The loop

```text
START -> OBSERVE -> PLAN --proposal--> POLICY --ALLOW--> EXECUTE -> VERIFY --+
            ^         |                   |                                 |
            |         |                   \--deny/approval-needed--> UPDATE <+
            |         |                                                |
            |         \--no proposal--> REPLAN <-----------------------+
            |                             |
            \--------- another turn ------+--- terminal --> END
```

| Node | Responsibility |
|---|---|
| OBSERVE | schedule an agent; project the graph into a bounded `GraphObservation` |
| PLAN | frontier -> planner -> one `ActionProposal` (+ `Hypothesis`) |
| POLICY | hand it to `ControlPlane.evaluate`; nothing else |
| EXECUTE | ALLOW-only: build the `ToolInvocation`, call `ToolGateway` |
| VERIFY | append Evidence, then reach a `Verdict` against the expectation |
| UPDATE | fold the outcome back into the Attack Graph |
| REPLAN | loop again, or stop with a named reason |

Three properties this shape is built around:

**Only ALLOW reaches Capability.** `router.route_after_policy` is the
single expression in the codebase that can route to `execute`, and it
reads `phase == EXECUTE`. Orchestration has no import path to a
`ToolGateway` implementation at all.

**A block is recorded, not skipped.** DENY and REQUIRE_APPROVAL route
through UPDATE. Nothing runs, but the attempt and the verdict land in
the graph — otherwise the report cannot say "policy stopped us looking
here", and the planner, seeing no attempt on record, re-proposes the
same action every turn.

**Every exit names itself.** `RuntimeState.termination_reason` is one of
`TerminationReason`: `no_actionable_plan`, `max_steps_reached`,
`budget_exhausted`, `consecutive_failures`, `no_agents_registered`.
"The run ended" and "the run ran out of budget after two turns" lead to
very different reports.

## Agents: declared playbooks, not free-form loops

```text
AgentSpec.playbook : list[PlanStep]      what this agent may ever propose
available_steps()  : the frontier        which of those the graph supports now
Planner.plan()     : the choice          which single one to propose this turn
```

A planner — including the LLM one — picks from `available_steps`. It
cannot invent a capability, an operation or a tool id that was never
declared, and whatever it picks still goes to the Control Plane. This
narrows what can be proposed; it never widens what is allowed.

A `PlanStep` either names a fixed `target`, or binds one from the graph:

```python
PlanStep(
    step_id="probe-host",
    capability="recon.example", operation="ping", tool_id=ToolId("example.ping"),
    target_bind="host",
    preconditions=[NodePattern(type=GraphNodeType.HOST, attrs={"in_scope": True}, bind="host")],
    hypothesis=HypothesisDraft(description="The host is reachable from the harness."),
    expectation=Expectation(kind="output_field_present", field="echo"),
    observed_attrs={"reachable": True},
)
```

The bound form is what makes the loop go somewhere: recon discovers
hosts, and the same step is then offered once per discovered host. A
step is dropped from the frontier once it has been attempted against
that target (`repeatable=True` opts out) — and "attempted" is read from
the Attack Graph, not a conversation buffer, so it survives a checkpoint
restore and is shared across agents.

### Adding an agent

1. Write an `AgentSpec` with its `playbook` in
   `orchestration/agents/registry.py` (or your own module).
2. Add it to the registry — order is the scheduling policy; each agent
   runs until it has nothing left to propose, then hands over.
3. Wire a non-default registry through `RuntimeDeps(agents=...)` in
   `app.bootstrap` if it should not be the shipped default.

No contract change is needed for any of this.

### Planners

`PlaybookPlanner` takes the first available step — deterministic, no
LLM, what most tests run against. `LLMPlanner` asks
`core.ports.llm.LLMClient` (LiteLLM behind it; no model name or provider
SDK appears in agent code) to choose a `step_id` from the candidates it
was shown. A `step_id` that was not offered is refused and the planner
falls back — recorded in `last_fallback_reason`, because a permanently
falling-back LLM planner would otherwise look exactly like a working
one.

The shipped Recon agent uses `planner_kind="llm"` so the LLM path stays
live in the default run. With `FakeLLMClient` it deterministically falls
back, so nothing is flaky.

## Verification: `Verdict` is not `ToolResult.success`

The skeleton mapped `success -> PASS`. `orchestration/expectations.py`
replaces that. An `Expectation` states, *before* the action runs, what
the result would have to look like and whether that outcome is benign or
a violation; it is stored on the `Hypothesis` in the graph, so it cannot
be reshaped around the output afterwards.

| Situation | Verdict |
|---|---|
| tool call failed | `UNKNOWN` — no verdict could be reached |
| no expectation under test | `PASS` |
| expectation met, `violation_if_met` | `VIOLATION` |
| expectation met, benign | `PASS` |
| expectation not met | `PASS` (or `ANOMALY` with `anomaly_if_unmet`) |
| expectation not evaluable | `UNKNOWN`, never a quiet `PASS` |

A `VIOLATION` produces a verified `Finding` carrying the `EvidenceId`
that supports it. `AttackGraphService.record_finding` refuses a verified
finding with no evidence, so an unfalsifiable claim cannot enter the
graph.

## State: ids, not objects

`RuntimeState` holds ids and counters (instruction.md §5). Domain state
lives in the Attack Graph and the Evidence Store and is referenced by
id — that is what keeps a checkpoint small enough to write every
super-step. Fields beyond §5 (`termination_reason`, `actions_executed`,
`consecutive_failures`, `finished_agents`) are all loop bookkeeping;
nothing a security judgement could rest on is in there.

`GraphState` additionally carries transient hand-offs between adjacent
nodes (`observation`, `plan_step`, `action_proposal`, `policy_decision`,
`tool_result`, `last_event_id`). They never leave this subpackage.

## Checkpointing and replay

`AgentLoopRunner` compiles the graph with an `InMemorySaver` and a
recursion limit derived from `max_steps` — LangGraph's default of 25
super-steps is under four turns of a seven-node loop, so a real run
would otherwise abort for no domain reason.

```python
runner = AgentLoopRunner(deps, settings=OrchestrationSettings(max_steps=20))
final = await runner.run(runtime)

history = await runner.state_history()          # newest first
resumed = await runner.resume_from(history[3].checkpoint_id)
```

Two things to know before relying on replay:

- **The allowlist matters.** LangGraph only reconstructs types on an
  explicit `(module, name)` allowlist; anything else comes back as a raw
  `dict`, silently. `checkpointing.state_type_allowlist()` derives it by
  walking `redteam.core`, `redteam.attack_graph` and
  `redteam.orchestration`, so it cannot go stale when a node puts a new
  model in the state.
- **Rewinding rewinds `GraphState` only.** The Attack Graph, the
  Evidence Store, and anything the target system saw are not rewound —
  by design, since evidence is append-only. A resumed branch continues
  against the world as it is now.

## Settings

`OrchestrationSettings` (ours) is separate from `core.config.OrchestrationConfig`
(the frozen shared contract) and bridges via `from_config`. It carries
`max_steps`, `max_retries`, `max_consecutive_failures`, observation
size, checkpointing, and the recursion limit. Everything in it is
execution policy — no value can widen what the Control Plane allows.

`app.bootstrap.run_engagement` seeds the engagement scope into the graph
before the loop starts (`orchestration/seed.py`): an empty graph is an
empty frontier, so without it the planner would have nothing to choose
from on turn one. Seeding a host makes it *visible*, not permitted — the
proposal against it is still evaluated.

---

## Contract Change Proposal (not applied)

**Current contract.** `core.ports.tool_gateway.ToolGateway` exposes
`invoke` only. There is no port through which orchestration can read the
Capability Registry.

**Problem.** A `PlanStep` has to hard-code its `tool_id`, and
`ToolDescriptor.risk_level` — which is the natural input to finding
severity (`orchestration/service_status.py`) — is unreachable from the
planning side. Today severity is a coarse constant.

**Proposed change.** Add a read-only port, e.g.
`core/ports/tool_catalog.py`:

```python
class ToolCatalog(Protocol):
    async def list_tools(self) -> list[ToolDescriptor]: ...
    async def get(self, tool_id: ToolId) -> ToolDescriptor | None: ...
```

**Affected.** Capability Team implements it on `MCPToolRegistry`; PM
adds the port; Agent Team consumes it in `agents/base.py` and
`service_status.py`. Additive — no existing call site changes.

**Alternative considered.** Passing descriptors in through `RuntimeDeps`
at bootstrap. Rejected: it makes the tool set static per run and hides a
Capability-owned concern inside Agent Team's dependency bundle.
