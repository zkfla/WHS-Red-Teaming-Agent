# ADR 0001: LangGraph as Workflow Runtime

## Status

Accepted.

## Context

We need an Agent orchestration runtime for a multi-step
Observe/Plan/Policy/Execute/Verify/Update/Replan loop, with the option
to re-plan and eventually branch across multiple agents.

## Decision

Use LangGraph (`langgraph.graph.StateGraph`) as the workflow runtime,
but treat it strictly as *runtime plumbing*: it owns node
sequencing/conditional edges, nothing else.

- `orchestration.state.RuntimeState` (a Pydantic model, defined in
  `core`-adjacent territory conceptually but living in `orchestration`
  since it's LangGraph-facing) is the durable slice of state — IDs
  only, per instruction.md section 5.
- `orchestration.state.GraphState` (a `TypedDict`) is what's actually
  passed through the compiled graph: `{"runtime": RuntimeState, ...transient fields}`.
  The transient fields (`action_proposal`, `policy_decision`,
  `tool_result`) exist only to move an in-flight object one hop
  between adjacent nodes in the same run; nothing outside
  `orchestration` reads them.
- Nodes are plain `async def fn(state: GraphState, *, deps: RuntimeDeps) -> GraphState`
  functions, bound to `deps` via `functools.partial` at graph-build
  time (`orchestration.graph.build_graph`) — they don't import
  LangGraph themselves.

## Consequences

- Domain state (Host/Credential/Finding/...) never gets duplicated into
  LangGraph's checkpointing — the Shared Attack Graph and Evidence
  Store stay the actual Source of Truth.
- If LangGraph is swapped for a different runtime later, only
  `orchestration.graph` and `orchestration.router` need to change —
  node function bodies and `RuntimeDeps`/`RuntimeState` don't, since
  they don't import `langgraph` themselves.
- Real LLM-driven planning/re-planning, deferred at ADR-writing time
  (instruction.md section 14), is no longer a stub: `orchestration.nodes.plan`
  calls a real `LLMPlanner`/`PlaybookPlanner` (`orchestration.agents.planners`)
  over registered specialist agents (`recon-agent`/`web-api-agent`/
  `mobile-agent`, `orchestration/agents/{registry,builtin}.py`), and
  `orchestration.nodes.replan` implements real convergence/stuck-detection
  (`orchestration.convergence`/`orchestration.progress`), not a single
  pass. What each agent's playbook can actually *do* once scheduled is
  still thin (see `docs/agent/orchestration-implementation-plan.md`
  section 16.1) — that is a separate, content-level gap from the
  runtime-plumbing question this ADR answers.
