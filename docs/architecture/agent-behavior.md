# Interactive Agent Runtime

## Previous runtime

The orchestration graph already enforced the safe execution chain:

```text
OBSERVE -> PLAN -> POLICY -> EXECUTE -> VERIFY -> UPDATE -> REPLAN
```

It also had an in-run operator channel for pause, resume, stop, focus,
exclude, status and approval. Three boundaries were incomplete:

1. `PolicyDecision` was retained long enough for `UPDATE`/`REPLAN`, then
   cleared. Its reasons and matched rules were not an explicit input to the
   next planner or LLM prompt.
2. Free-form operator input was rejected unless it matched the fixed control
   vocabulary. Strategy guidance such as "try another allowed method" could
   not influence the next decision.
3. Events existed, but the default CLI did not present them as one decision
   trace. In particular, planner input, selection rationale, policy rules,
   state deltas and the wait/termination reason were difficult to correlate.

## Current runtime

```text
operator input
  -> ConversationAssistant (route/clarify/answer) or OperatorIntentService
  -> Mission objective / control intent / bounded guidance
  -> OBSERVE (Attack Graph + Mission + recent runtime observations)
  -> PLAN LLM turn (public response + assumptions + action drafts)
     -> recipe provider only when the LLM call/drafts are not executable
  -> ActionProposal
  -> Control Plane
  -> PolicyDecision -> RuntimeObservation
  -> ALLOW: Tool/MCP -> ToolResult -> Evidence -> Attack Graph
     DENY: no tool -> blocked attempt -> Attack Graph
     REQUIRE_APPROVAL: suspend exact proposal -> operator -> POLICY resume
  -> RuntimeObservation window
  -> next planner/LLM context
  -> CLI decision trace
```

### Runtime observations

`RuntimeState.recent_observations` is a bounded window of 12 compact,
redacted `RuntimeObservation` records. It carries only decision-relevant
feedback that is not naturally represented by the Attack Graph:

- Control verdict, reasons, matched rules and approval identity;
- tool success/failure and stable error code;
- state-update outcome and graph-mutation counts.

Raw prompts, tool output, arguments and credentials are never stored in this
window. Attack Graph and Evidence remain the canonical domain stores.

Both `LLMCandidateProvider` and the LLM-assisted planner receive this window.
Consequently a denial is not presented as a generic tool failure: the next
turn can distinguish policy denial, approval suspension, execution failure
and a successful observation.

### Conversation and operator input

Known control utterances remain deterministic and auditable. Other free-form
messages go to the configured LLM, which returns one of `execute`, `answer`,
`clarify` or `guidance` plus a displayable response, assumptions and plan.
During an active Run, execution-shaped chat becomes redacted, bounded Mission
guidance. It may influence planning but cannot add an unknown target,
construct a tool invocation, expand scope, or bypass candidate compilation
and Control.

The UI never asks the model for private chain-of-thought. It asks for a short
decision summary and displays the actual Observation, selected MCP binding,
Control verdict, Tool result and State delta as the auditable reasoning trail.

### Planning modes and the playbook

The playbook is no longer the default action menu for a real CLI LLM.
`llm_first` gives the model the healthy live MCP action space and lets its
structured turn propose any action inside the current role's capabilities.
The candidate compiler then verifies target identity, canonical action names,
tool binding, health, target type and argument schema. Control remains the
only authorization boundary.

Playbooks remain useful as tested recovery recipes: if the LLM call fails or
its `act` drafts cannot compile, the role provider can still make safe
progress. `hybrid` retains the old playbook-plus-LLM union for compatibility;
`playbook_only` is the offline deterministic mode.

The CLI accepts control words with or without a leading slash while a run is
active. After a run, bare `status`, `why`/`explain`, `approve`/`deny` and
`stop` are handled as session commands instead of accidentally creating a new
Mission.

### Decision trace

The default Rich renderer emits a concise trace from structured events:

```text
[User] inspect localhost:5000
[Agent] HTTP service 여부를 확인하기 위해 passive probe를 실행하겠습니다.
[Reason] 현재 graph에는 host만 있고 service 관찰이 없습니다.
[Observation] step=1 nodes[host=1] attempted=0
[Agent] Probe the declared host -> service_discovery (single)
[MCP] recon-passive/http_probe selected (risk=low, health=healthy)
[Action] Recon Agent: Discovering services on localhost:5000
[Control] ALLOW — within scope; policy=scope, budget
[Tool Result] Tool call succeeded
[State] Graph mutation applied: 2 node(s), 1 edge(s)
[Next] continue (graph_changed)
```

`AgentTurnDecided` reports the model's public response, assumptions and short
decision summary. `AgentContextPrepared` reports the bounded inputs made
available to planning.
`AgentDecisionRecorded` reports the chosen operation, selection basis and a
short rationale. Neither event contains hidden chain-of-thought. Verbose mode
still exposes internal ids and the complete redacted event payload for
debugging.

### Suspension and termination

An approval is a suspension, not a completed run. The exact proposal and plan
step remain in the checkpoint; approval reconciliation re-enters at `POLICY`
and never creates a replacement action. Pause behaves similarly at Mission
level. Hard stops, explicit operator stop, exhausted budget and convergence
remain terminal and are rendered with their named reason.

The interactive CLI session outlives a single Run. A completed Run returns to
`operator>`; `/exit`, bare `exit`/`quit`, or EOF closes the session.

## Remaining constraints

- Free-form chat is interpreted by the LLM, but runtime guidance remains
  bounded; chat is not a direct tool-execution endpoint.
- The in-memory Event Bus history is diagnostic, not durable. Durable recovery
  relies on checkpoints, Task/Mission stores, Evidence, Attack Graph and audit
  logs.
- A completed Run starts a new Mission when asked to continue. The engagement-
  scoped Attack Graph survives, but run-scoped evidence excerpts are not yet
  merged into a cross-run conversation transcript.
- Live MCP bindings and their schemas determine which model-proposed actions
  can compile. Result mappers still determine which tool-specific output
  shapes become typed Attack Graph mutations.
- Default MCP bootstrap registers the read-only recon/web baseline only.
  Active, proxy, mobile and artifact-analysis servers still need their
  operator-owned prerequisites.
