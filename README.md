# Generic Red-Team Core

This workspace is a fresh, domain-neutral reconstruction from three donor agents:

- CI/CD: canonical evidence/relation/knowledge, investigation and bounded reasoning mechanics.
- Financial: provenance discipline, state/experiment/policy/oracle mechanics.
- Mobile: bounded artifact acquisition and static-to-runtime verification mechanics.

The donor repositories are not runtime dependencies. Domain semantics such as GitHub Actions,
financial accounts, Android components, HTTP endpoints, AWS, ADB, or JADX do not belong in the
core package.

## Current implementation status

Batch A+B+C+D+E+F implement Phases 0-14 of `docs/GENERIC_CORE_REFACTOR_PLAN.md`:

1. donor source freeze and conformance inventory,
2. stale generic workspace reset,
3. foundational strict models/provenance/evidence contracts,
4. typed bundle/schema registries with frozen semantic manifests,
5. generic evidence requirements, deterministic multi-resolver selection/fallback, permission and
   call-budget gates, typed observations with snapshot/expiry validity, conflict-preserving knowledge
   revision, atomic semantic commits and a generic Relation recompute seam.
6. registered/frozen RelationSemantics plus a bounded generic Frontier with guards, context bridges,
   dominance, diversity and explicit completeness/truncation metadata.
7. Candidate-first derivation, qualified root-cause identity, durable coverage accounting,
   family-diverse bounded portfolios and multi-batch terminal rotation.
8. immutable artifact-root confinement, bounded inventory/search/read, explicit acquisition budgets,
   advisory-only hints and typed untrusted-artifact results.
9. revision-CAS WorkingMemory with canonical grounding/reconciliation, bounded/redacted working text,
   deterministic six-channel context projection and fail-closed history compaction.
10. manifest-owned validator applicability, exact run-all execution, canonical-result grounding,
    durable validation outcomes and deterministic 0..N Finding materialization.
11. typed controlled experiments with immutable active policy snapshots, manifest-bound executor and
    constraint evaluators, permit-gated final effect execution, durable attempt/retry ambiguity and
    explicit cleanup accounting.
12. atomic checkpoint/resume with a hash-chained fsync event journal, resolver-routing-compatible
    runtime fingerprints, opaque bundle-owned domain-state codecs, process-level single-writer lease,
    crash-cut `UNCERTAIN` recovery, deterministic termination/budgets and one cleanup-aware finalizer.
13. typed normative expectation ledger consumed by validators, expectation-revision validation identity,
    and an explicit raw-evidence retention/export/LLM policy boundary.
14. four heterogeneous synthetic reference domains (`static_graph`, `stateful_experiment`,
    `artifact_runtime`, `request_response`) with no reference-specific core edits.
15. deterministic multi-domain composition/integration proof and final locked-donor conformance closure.

The Generic Core scope defined by the refactor plan is complete. Concrete GitHub/financial target,
Mobile device and Web adapters remain separate domain/bundle implementation work; this repository does
not claim those concrete adapters or real exploit capabilities are implemented.

## Development

```text
uv sync --dev
uv run python -m pytest
uv run ruff check src tests
```

## Domain-neutral performance/runtime follow-up

The separate [performance evolution plan](docs/GENERIC_CORE_PERFORMANCE_EVOLUTION_PLAN.md)
adds operation-local lookup, bounded Frontier work, durable request accounting,
passive result receipts/batches, focused evidence context, a provider-neutral step
loop, and evidence-bound priority history. G0-G4/G6-G9 are **PASSED_OFFLINE**;
G5 and C1-C4 are conditionally deferred by the plan's untriggered gates.
See the [requirement audit](docs/GENERIC_CORE_PERFORMANCE_ACCEPTANCE.md) and
[G9 measurement record](docs/GENERIC_CORE_G9_RECEIPT.md).

Public integration boundaries (explicit trusted composition, no auto-discovery):

| Purpose | Module/API |
|---|---|
| Atomic knowledge and freshness | `redteam_core.knowledge.apply_observations_atomic`, `refresh_freshness` |
| Bounded search | `redteam_core.graph.frontier.FrontierQuery`, `build_attack_frontiers` |
| Durable passive requests | `redteam_core.runtime.resolver_dispatch.DurableResolverDispatcher` |
| Bounded batch/single-flight | `redteam_core.runtime.passive_scheduler.PassiveResolutionScheduler` |
| Focused evidence projection | `redteam_core.reasoning.project_focused_decision_context` |
| Typed proposals and driver protocol | `redteam_core.models.decision.DecisionProposal`, `redteam_core.runtime.step_loop.DecisionDriver` |
| Bounded investigation steps | `redteam_core.runtime.step_loop.GenericStepEngine` |
| Advisory ordering | `redteam_core.investigation.priority.BaselinePriorityProvider`, `ExpectedUtilityPriorityProvider` |
| Durable selection/host decision binding | `redteam_core.runtime.priority.PriorityCoordinator` |

No real model/provider implementation is bundled. The step engine takes a supplied
driver and rechecks proposal refs, budget and authority; only test drivers are
provided here. It uses focused context within this explicitly composed path.
Legacy APIs remain available. Host terminal decisions do not create Findings or
replace run-all-applicable validators. Ranking is not an execution permit.

The approved 0.4.1 offline release preserves full-scan lookup, memo off, legacy query
budget, serial passive scheduling and baseline priority. The provisional memo
default was withdrawn after a final control fixture's p95 regression. Both legacy
and v2 support explicit `memoize_sink=True`; `False` remains the rollback.
Alternative paths are opt-in;
not every measured optimization qualifies for default enable. In particular,
the utility provider regressed two fixed-budget coverage fixtures and is withheld.

Runtime/package **0.4.1**, checkpoint schema **4.0**: incompatible older checkpoints
are rejected, not silently migrated. Do not delete ledgers to reset budget/aging,
or downgrade a writer onto a newer checkpoint. Rollback uses the current runtime's
legacy/full-scan/serial/baseline options while retaining durable receipts/history.
The runtime is single-writer. It is not a sandbox for malicious Python plugins.

Derived Observation indices admit at most 100,000 memberships and 8 MiB of
membership reference IDs. Overflow uses a complete full scan, never a partial
index masquerading as complete evidence. These are auxiliary structure caps,
not limits on canonical ledger size, payload retention, or total process RSS.

See `docs/SOURCE_EXTRACTION_MAP.md`, `docs/SOURCE_CONFORMANCE_MATRIX.md` and
`docs/SOURCE_BASELINE_RECEIPT.md` for donor traceability. Batch receipts are in
`docs/BATCH_A_IMPLEMENTATION_RECEIPT.md`, `docs/BATCH_B_IMPLEMENTATION_RECEIPT.md`,
`docs/BATCH_C_IMPLEMENTATION_RECEIPT.md`, `docs/BATCH_D_IMPLEMENTATION_RECEIPT.md`,
`docs/BATCH_E_IMPLEMENTATION_RECEIPT.md` and `docs/BATCH_F_IMPLEMENTATION_RECEIPT.md`.
