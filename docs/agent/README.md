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
| [orchestration-implementation-plan.md](orchestration-implementation-plan.md) | benchmark-backed target design, phases, team contracts and completion criteria |
| [attack-graph.md](attack-graph.md) | node identity, predicates, preconditions, integrity, snapshots |
| [event-bus.md](event-bus.md) | envelope construction, correlation, what each node emits |

These pages document Agent Team's *implementation*. The shared contracts
they build on are PM's and live in `docs/contracts/` and
`docs/architecture/`; nothing here changes them.

## What holds the boundary

- `redteam.core` is read-only for this team. `core/contracts`,
  `core/ports`, `core/ids.py`, `core/enums.py` are Contract v0.1 — a
  change goes through the Issue -> Proposal -> PM Review flow in
  `docs/contracts/versioning.md`. No frozen Core contract is modified by the
  orchestration implementation.
- Orchestration reaches Control and tool execution through `core.ports`
  Protocols. Catalog reads use an Agent-Team-owned structural subset that the
  Capability registry already satisfies; the compatibility boundary is
  documented in [orchestration.md](orchestration.md).
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
| Approval resume | Control port cannot retrieve/consume a resolved approval; orchestration must not infer `ALLOW` from an event |
| Long-running reconciliation host | one safe `reconcile_once()` tick exists; deployment must host repeated ticks |
| Tool retries | retryable/idempotency and budget semantics belong in the Capability gateway contract; orchestration does not duplicate an approved side effect |
| Mobile asset scope | Control cannot yet compare `AssetTarget` identity with engagement scope, so it correctly denies Mobile execution until the PM/Control contract is extended |
| Shared graph tenancy | run-scoped ledger/evidence/reporting are isolated; a shared Attack Graph still needs a PM/Graph-owned engagement namespace |
| Graph-DB backend | `InMemoryAttackGraphStore` only (deferred, instruction.md §21) |
| Cleanup execution | the graph tracks what is owed; actually removing a resource is an Orchestration action |
| Retraction rules | three conservative defaults in `attack_graph/retraction.py`; tune per engagement |
