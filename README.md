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
uv run pytest
uv run ruff check src tests
```

See `docs/SOURCE_EXTRACTION_MAP.md`, `docs/SOURCE_CONFORMANCE_MATRIX.md` and
`docs/SOURCE_BASELINE_RECEIPT.md` for donor traceability. Batch receipts are in
`docs/BATCH_A_IMPLEMENTATION_RECEIPT.md`, `docs/BATCH_B_IMPLEMENTATION_RECEIPT.md`,
`docs/BATCH_C_IMPLEMENTATION_RECEIPT.md`, `docs/BATCH_D_IMPLEMENTATION_RECEIPT.md`,
`docs/BATCH_E_IMPLEMENTATION_RECEIPT.md` and `docs/BATCH_F_IMPLEMENTATION_RECEIPT.md`.
