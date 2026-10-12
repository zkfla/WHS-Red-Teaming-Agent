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
| [orchestration.md](orchestration.md) | the current central planner, investigations, parallel workers and exact resume |
| [orchestration-implementation-plan.md](orchestration-implementation-plan.md) | benchmark-backed target design, phases, team contracts and completion criteria |
| [2026-10-04 modularization plan and work records](modularization-20261004/README.md) | provider assembly implementation, audits, independent reviews and final CI results |
| [attack-graph.md](attack-graph.md) | retained graph writes/reads, provenance, integrity and internal storage |
| [event-bus.md](event-bus.md) | envelope construction, correlation, what each node emits |

The current runtime reference is `orchestration.md`, `attack-graph.md` and
[`central-state.md`](../contracts/central-state.md). Dated plans, work logs and
acceptance reports are historical records; their old queues, graph renderers or
completion rules are not descriptions of the current default runtime.

These pages document Agent Team's *implementation*. The shared contracts
they build on are PM's and live in `docs/contracts/` and
`docs/architecture/`; nothing here changes them.

## What holds the boundary

- `core/contracts` and `core/ports` own shared meanings. Central storage implements
  these contracts; orchestration does not replace Control permission with planning.
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
| Approval resume | **Resolved.** `ControlPlane.approval_outcome(approval_id)` (read-only Core Port) resumes the same Run via a conditional START edge straight into POLICY — see `docs/agent/orchestration-implementation-plan.md` section 16. |
| Long-running reconciliation host | Removed (`orchestration.reconciliation.run_reconciliation_loop` had no real runtime/CLI caller — it was never wired into any host process; `AgentLoopRunner.reconcile_once()` itself is unaffected and still drives the actual Approval-resume/polling paths in `commands/run.py`). A standalone repeating host loop remains a deployment-layer choice if one is ever needed. |
| Tool retries | retryable/idempotency and budget semantics belong in the Capability gateway contract; orchestration does not duplicate an approved side effect |
| Mobile asset scope | **Resolved.** `EngagementScope.allowed_assets`/`denied_assets` (exact `asset_id` match, `redteam.control.scope._check_asset_scope`) lets Mobile actions ALLOW explicitly; still fail-closed DENY outside scope. |
| Shared graph tenancy | **Resolved.** `attack_graph.stores.namespaced.EngagementScopedAttackGraphStore` — a decorator over the existing `AttackGraphStore` Core Port, namespacing ids at the store boundary; `app.bootstrap` wires it in by default. |
| Graph storage | Central SQLite by default, with graph mutation receipts in the same transaction as execution/result changes. |
| Cleanup execution | Provider resource-effect metadata connects actual creation/removal originals to cleanup obligations; undeclared effects are reported as unmapped. |
| Retraction rules | three conservative defaults in `attack_graph/retraction.py`; tune per engagement |
| New graph/context API | A relation graph spanning Tasks/results/questions and a unified context API are follow-up scope. |
