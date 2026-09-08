# Batch F Implementation Receipt — Phase 12~14

Date: 2026-09-08

## Result

`COMPLETE` for Batch F and for the Phase 0~14 Generic Core scope defined by
`GENERIC_CORE_REFACTOR_PLAN.md`. The deterministic tree/plan hashes at the end of this receipt are
finalized against the synchronized Batch-F documents and source/test trees.

This completion does **not** claim concrete GitHub/financial target/Mobile device/Web adapters or real
exploit functionality are implemented. Batch F proves the domain-independent core can host those shapes
without absorbing their concrete domain models.

## Locked donor commits

| Donor | Commit | Final status |
|---|---|---|
| CI/CD | `ee89e3063dc5f25fb19af950e4a3e4023dbd6d5d` | clean |
| Financial | `83885a478b9151ab4be652caff6ba564dce8ee6e` | clean |
| Mobile | `f1ed305fb384de7e0043df8fc5e01f4935ab4689` | clean |

No donor source file was modified by Batch F.

## Pre-reference acceptance closure

The final acceptance audit intentionally did not treat previously incomplete contracts as complete. It found
and closed three generic gaps before finalizing the reference proof:

1. `ExpectationRef` became a canonical provenance-bound identity with `ExpectationState` and
   `record_expectation()`. Inferred and authoritative normative material can coexist without trust
   collapse.
2. `ValidationRunRecord` now binds `expectation_revision`; changing validator-relevant normative state
   cannot silently reuse an old validation snapshot. Candidate-grounded expectation evidence can enter
   the validator's canonical evidence allowance.
3. `redteam_core.evidence.storage.authorize_raw_artifact_use()` is the designated fail-closed boundary
   for raw artifact retention, export and LLM exposure under `EvidenceStoragePolicy`. Storage backends
   remain adapter-owned.

After those generic acceptance fixes, the final core source tree was frozen at:

```text
redteam_core source SHA256:
53e814dcb5152fa178765201a9afa22c22eab928b3ba3b2101645b738cd1c21c
```

The Batch-F reference integration test recomputes this hash and fails if reference-specific work changes
the core afterwards.

## Phase 12 — Four reference domains

Implemented under `tests/reference_domains/` only:

### `static_graph`

- UNKNOWN relation uncertainty,
- MAY_BLOCK guard,
- context bridge,
- bounded Frontier,
- Candidate-first derivation,
- root-cause portfolio and terminal coverage.

### `stateful_experiment`

- pathless Candidate,
- inferred expectation is not normative authority,
- controlled in-memory state mutation,
- typed policy constraint,
- ExperimentResult,
- no authoritative baseline => `UNKNOWN`,
- authoritative baseline + successful but normatively wrong transition => `VIOLATION` + Finding.

### `artifact_runtime`

- bounded artifact search,
- `UNTRUSTED_ARTIFACT`,
- advisory-only AnalysisHint,
- static relation remains UNKNOWN,
- typed runtime requirement/resolver Observation,
- deterministic TRUE recompute,
- Candidate and WorkingMemory/coverage reconciliation.

### `request_response`

- request/authorization-shaped semantics without HTTP concrete core types,
- synthetic not-found => `AMBIGUOUS_NOT_FOUND`, never SAFE/FALSE,
- read-only policy rejects a state-changing action,
- explicitly approved state-changing experiment,
- typed Observation + authorization `VIOLATION` validator.

`tests/integration/test_batch_f_reference_domains.py` reference/composite suite: **7/7 PASS**.

## Phase 13 — composite proof

The four references and `refintegration` are composed in one runtime:

- normal and reversed bundle registration order yield the same semantic manifest,
- integration dependencies are declared with `requires_bundle_ids`,
- integration source imports no sibling private reference implementation/state,
- removing the static bundle leaves other bundle registrations and actual stateful Candidate semantics
  unchanged,
- multi-deriver Candidate generation and bounded portfolio are deterministic,
- validator repeated snapshot is idempotent,
- multi-bundle checkpoint write/load round-trips canonical Candidate state,
- Observation → Relation → Candidate → WorkingMemory/Coverage deterministic convergence is exercised.

## Phase 14 — donor conformance closure

### CI/CD

Locked donor focused mandatory suites:

```text
test_domain.py
test_investigation_runtime.py
test_evidence_resolvers.py
test_graph_frontier.py
test_investigation_policy.py
test_working_memory.py
test_validator.py
test_security_candidates.py

94 passed
```

### Financial

Run with an ephemeral `uv run --no-project` dependency environment so the donor is not modified:

```text
test_graph.py
test_oracles.py
test_execute_spine.py
test_concurrency.py
test_engagement_policy.py
test_policy_enforcement.py
test_cleanup_tracking.py
test_termination_policy_stop.py

87 passed
```

### Mobile

The locked Mobile donor still has no `tests/` directory. A donor-read-only temporary-tree
characterization rechecked:

- root escape rejection,
- `list_files` 200 clamp,
- `search_text` 100 clamp,
- >2,000,000-byte search skip,
- read truncation at the donor cap,
- preprocess candidates are hints, not hard scope,
- artifact natural language is untrusted data,
- static suspicion is explicitly verified through dynamic tooling.

Result:

```text
MOBILE_CHARACTERIZATION_PASS
```

## Final Generic Core release gates

Latest code before final document synchronization:

```text
uv run pytest -o addopts=''                         175 passed
uv run ruff check src tests                         PASS
uv run python -m compileall -q src tests            PASS
uv build                                            PASS
PYTHONHASHSEED=1 runtime checkpoint suite            24/24 PASS
PYTHONHASHSEED=7 runtime checkpoint suite            24/24 PASS
PYTHONHASHSEED=777 runtime checkpoint suite          24/24 PASS
four-reference/composite suite                       7/7 PASS
core reference-freeze SHA gate                       PASS
architecture/domain contamination gates              PASS
donor HEAD/dirty                                     PASS / clean
```

Reference-suite local wall-clock smoke baseline:

```text
tests/integration/test_batch_f_reference_domains.py
7 passed in 0.81s
```

This is a host-local, non-normative regression reference only. It is not a claim that the generic core
is faster than any donor implementation and is not a portable performance SLO.

## Scope intentionally outside this completion

- concrete GitHub/GitHub Actions domain bundle and live resolvers,
- concrete financial target/bank operation adapters,
- concrete Mobile APK/JADX/ADB domain adapter,
- concrete Web/browser/network adapter,
- malicious/untrusted third-party Python plugin sandboxing,
- real exploit execution against external targets.

These are domain/adaptor or separate security-boundary work. They are not hidden prerequisites for the
Phase 0~14 domain-independent core acceptance defined by the plan.

## Deterministic receipts

Finalized after document synchronization and the final release gates:

```text
src+tests tree SHA256: 0d1372f77e6e6391fd030ca3a47155e046451447817a5082a572de3bfdb1958b
GENERIC_CORE_REFACTOR_PLAN.md SHA256: cc636d9ec57f5530467fd45ccc4d3162e4b6a84f3a67541a8d73a685c06b4104
```
