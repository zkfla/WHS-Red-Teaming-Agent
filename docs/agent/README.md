# Agent Team

Owner of three modules: **Agent Orchestration**, the **Shared Attack
Graph**, and the **Event Bus**.

```text
src/redteam/orchestration/   LangGraph workflow, agents, planners, re-plan loop
src/redteam/attack_graph/    security domain state (the Source of Truth)
src/redteam/event_bus/       EventEnvelope transport + publisher
```

| Page | What it covers |
|---|---|
| [orchestration.md](orchestration.md) | the loop, agents/playbooks, planners, verification, checkpointing |
| [attack-graph.md](attack-graph.md) | node identity, predicates, preconditions, integrity, snapshots |
| [event-bus.md](event-bus.md) | envelope construction, correlation, what each node emits |

These pages document Agent Team's *implementation*. The shared contracts
they build on are PM's and live in `docs/contracts/` and
`docs/architecture/`; nothing here changes them.

## What holds the boundary

- `redteam.core` is read-only for this team. `core/contracts`,
  `core/ports`, `core/ids.py`, `core/enums.py` are Contract v0.1 — a
  change goes through the Issue -> Proposal -> PM Review flow in
  `docs/contracts/versioning.md`. Two proposals from this pass are
  written up at the bottom of [attack-graph.md](attack-graph.md) and
  [orchestration.md](orchestration.md); neither is applied.
- Orchestration reaches Control and Capability **only** through
  `core.ports` Protocols. `tests/unit/test_control_plane_bypass.py`
  checks that statically.
- Orchestration, Attack Graph and Event Bus may import each other —
  same team, per `docs/architecture/dependency-rules.md`.

## Running it

```bash
python -m redteam.app.cli run examples/engagement.yaml
pytest tests/unit tests/integration -q
```

## Still open

| Area | State |
|---|---|
| Web/API, Mobile, Reporting agents | only the Recon agent ships; add an `AgentSpec` to `agents/registry.py` |
| Retry on a transient DENY | `max_retries` / `retry_count` exist, nothing consumes them |
| Durable checkpointer | `InMemorySaver` only; see `orchestration/checkpointing.py` |
| Cleanup of created resources | no lifecycle for artifacts an action leaves behind |
| Graph-DB backend | `InMemoryAttackGraphStore` only (deferred, instruction.md §21) |
| Severity scoring | coarse default — see `orchestration/service_status.py` |
