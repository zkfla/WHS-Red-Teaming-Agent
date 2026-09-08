# Batch B Implementation Receipt — Phase 5~6

Date: 2026-09-08

## Result

`COMPLETE` for Batch B as defined in `GENERIC_CORE_REFACTOR_PLAN.md`.

This receipt means Phase 5 (generic RelationSemantics / Frontier) and Phase 6
(Candidate-first / root-cause coverage) are implemented and the current release gates pass.
It does **not** claim that later acquisition, working-memory/context, validator/oracle execution,
controlled experiment, or durable checkpoint phases are implemented.

## Locked donor commits

| Donor | Commit | Final status |
|---|---|---|
| CI/CD | `ee89e3063dc5f25fb19af950e4a3e4023dbd6d5d` | clean |
| Financial | `83885a478b9151ab4be652caff6ba564dce8ee6e` | clean |
| Mobile | `f1ed305fb384de7e0043df8fc5e01f4935ab4689` | clean |

No donor source file was modified by Batch B.

## Phase 5 — Generic RelationSemantics / Frontier

Primary files:

```text
src/redteam_core/graph/semantics.py
src/redteam_core/graph/frontier.py
src/redteam_core/graph/__init__.py
src/redteam_core/extension/registry.py
src/redteam_core/extension/bundle.py
```

Implemented:

- frozen registered `RelationSemantics`
- semantic manifest binding for relation semantics
- source / mechanism / sink semantic tags
- guard effects: `BLOCKS`, `MAY_BLOCK`, `INFORMATIONAL`
- context-scoped guard applicability
- explicit context-bridge semantics
- fail-closed non-traversal for unregistered relation types
- deterministic bounded graph traversal
- cycle avoidance
- dominance pruning, including stale queued-state recheck
- bounded diversity selection
- max-depth / max-expansion / max-frontier limits
- explicit completeness, truncation and depth-limit metadata
- registered intermediate-sink continuation for focused search
- certainty ceiling based on FALSE / UNKNOWN / guard state
- no CI/CD relation/source/context special-case in the generic Frontier engine

The donor's useful graph-search mechanics were preserved, while concrete GitHub job/trigger,
permission, guard-name and attacker-source special cases were not copied into core.

## Phase 6 — Candidate-first / Coverage

Primary files:

```text
src/redteam_core/investigation/candidates.py
src/redteam_core/investigation/coverage.py
src/redteam_core/models/security.py
src/redteam_core/knowledge/state.py
src/redteam_core/investigation/__init__.py
src/redteam_core/extension/registry.py
src/redteam_core/extension/bundle.py
```

Implemented:

- `CandidateDeriver` protocol/runtime registry
- `CandidateDeriverRegistration`
- CandidateDeriver implementation fingerprint in the frozen semantic manifest
- runtime deriver set must exactly match the frozen manifest
- Candidate derivation directly from canonical semantic state, without Finding materialization
- namespaced `candidate_type_id`
- qualified `RootCauseRef(owner_bundle_id, family_id, key)`
- deterministic candidate semantic identity
- pathless state/invariant Candidate support
- grounding validation against current canonical Relations
- `CandidateState` and durable `CoverageLedgerEntry`
- atomic CandidateState reconciliation
- disappeared-candidate terminal history preservation
- one representative per non-terminal root cause
- family-diverse bounded portfolio selection
- deterministic tie-breaking
- multi-batch root rotation until all active roots are terminal
- one terminal decision applies to all candidate variants of the same root
- portfolio binding to candidate revision + knowledge revision
- stale portfolio rejection
- duplicate batch-record rejection
- terminal commit before batch record rejection
- exactly one terminal decision per selected root
- current candidate / coverage-ledger cross-invariants

## Generic Batch B integration proof

`tests/integration/test_batch_b_spine.py` exercises the complete Batch B reasoning slice:

```text
Relation graph
→ Generic Frontier
→ CandidateDeriver
→ SecurityCandidate
→ qualified RootCause
→ bounded Coverage portfolio
→ batch record
→ terminal root decision
→ coverage complete
```

This proves the generic decision plane is Candidate-first rather than the legacy
`Finding/AttackPath -> Candidate` migration path.

## Generic test result

Commands:

```text
uv run ruff check src tests
uv run pytest -ra -o addopts=''
uv run python -m compileall -q src
uv build
```

Result:

```text
ruff:       All checks passed
pytest:     75 passed
compileall: PASS
build:      source distribution + wheel PASS
domain scan: PASS
donor drift/dirty check: PASS
```

Important Batch B regression coverage includes:

- Frontier ready/blocked/unknown behavior
- guard context isolation
- explicit context bridging
- focused continuation past intermediate sink
- deterministic bounded/truncated/depth-limited search
- fail-closed unknown relation semantics
- explicit dominance pruning
- source-bucket diversity preservation
- Finding-free Candidate derivation architecture gate
- root family diversity
- 9 roots / portfolio limit 3 multi-batch terminal drain
- all variants of one root receive one root decision
- disappeared Candidate accounting preservation
- stale portfolio fail-closed behavior
- duplicate/unrecorded batch lifecycle rejection
- CandidateDeriver semantic-manifest compatibility binding

## CI/CD donor conformance regression

The locked CI/CD donor's relevant original suites were re-run after Batch B implementation:

```text
tests/unit/test_graph_frontier.py
tests/unit/test_investigation_policy.py
tests/unit/test_security_candidates.py

32 passed in 5.75s
```

This is a donor-behavior regression check, not a claim that the generic implementation is
source-identical to CI/CD. The generic code intentionally removes donor vocabulary and domain coupling.

## Architecture gates

The current architecture tests additionally verify:

- `redteam_core` imports none of the donor packages,
- no hidden wall-clock calls exist in semantic code outside the explicit clock adapter,
- project dependencies remain domain-neutral,
- no known CI/CD / Financial / Mobile donor vocabulary leaks into core/application source,
- the generic Frontier contains no namespaced relation-type literal hardcoding,
- Candidate derivation does not import/use Finding materialization as an input dependency.

## Deterministic receipts

Filled after the final document synchronization and source/test hash pass:

```text
src+tests tree SHA256: 5ad46fe885ab727e5b36e2a0038434186cbdd8f2a9f34571e28fd27ea96946b0
GENERIC_CORE_REFACTOR_PLAN.md SHA256: 19a456757e7e1d08515721f3a89558fb7bb44315bb3815bcc7a6c7b1ae093aac
```

## Known intentionally-unimplemented work

Batch B does not implement:

- Phase 7 ArtifactBoundary / bounded acquisition,
- Phase 8 WorkingMemory / context projection,
- Phase 9 validator/oracle execution authority,
- Phase 10 controlled experiment / effect boundary / cleanup,
- Phase 11 durable checkpoint/resume runtime,
- final reference-domain and composite proofs.

Those remain Phase 7 onward and must build on the typed registries, canonical knowledge,
RelationSemantics, Candidate-first plane and coverage invariants completed in Batch A+B.
