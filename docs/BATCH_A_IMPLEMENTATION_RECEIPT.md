# Batch A Implementation Receipt — Phase 0~4

Date: 2026-09-07

## Result

`COMPLETE` for the scope defined as Batch A in `GENERIC_CORE_REFACTOR_PLAN.md`.

This receipt means Phases 0-4 are implemented and their current gates pass. It does **not** claim
that later Frontier, coverage, acquisition, validator execution, experiment, or checkpoint phases
are implemented.

## Locked donors

| Donor | Commit | Final status |
|---|---|---|
| CI/CD | `ee89e3063dc5f25fb19af950e4a3e4023dbd6d5d` | clean |
| Financial | `83885a478b9151ab4be652caff6ba564dce8ee6e` | clean |
| Mobile | `f1ed305fb384de7e0043df8fc5e01f4935ab4689` | clean |

No donor source file was modified by the migration. A generated Financial `uv.lock` created by a
baseline test bootstrap was removed before implementation continued.

## Phase 0

Created:

- `docs/SOURCE_EXTRACTION_MAP.md`
- `docs/SOURCE_CONFORMANCE_MATRIX.md`
- `docs/SOURCE_BASELINE_RECEIPT.md`

Donor baseline results:

```text
CI/CD focused baseline:   85 passed in 17.44s
Financial focused baseline: 89 passed in 30.79s
Mobile characterization: MOBILE_CHARACTERIZATION_PASS
```

## Phase 1

Verified generic root before deletion:

```text
C:\Users\qkrgu\Documents\GitHub\WHS-Red-Teaming-Agent-generic
```

Deletion inventory:

```text
old src:   117 files / 2,108,086 bytes
old tests: 264 files / 2,355,258 bytes
```

Only `generic\src` and `generic\tests` were recursively removed. Source donor repositories were
outside the deletion targets.

Regenerated:

- `pyproject.toml`
- `README.md`
- `PLANS.md`
- `.env.example`
- `uv.lock`
- new `src/redteam_core`, `src/redteam_app`, `tests` trees

The dependency set is now domain-neutral: runtime dependency is Pydantic only.

## Phase 2 implementation

Primary files:

```text
src/redteam_core/models/base.py
src/redteam_core/models/provenance.py
src/redteam_core/models/security.py
```

Implemented:

- strict `SchemaModel(extra=forbid)`
- deterministic semantic `stable_id`
- canonical commit/recompute boundary에서 requirement/observation/relation ID binding 재검증
- `TriState(TRUE/FALSE/UNKNOWN)`
- `TrustClass` including non-authoritative `INFERRED`
- `Provenance`
- canonical `Evidence`
- raw `EvidenceArtifactRef` separated from canonical evidence
- fail-closed `EvidenceStoragePolicy`
- typed/hashed `TypedPayloadEnvelope`
- generic `Relation` + typed missing reason
- `ExpectationRef`
- claim ceiling and `SecurityCandidate`
- validation outcome and finding result contracts

## Phase 3 implementation

Primary files:

```text
src/redteam_core/extension/registry.py
src/redteam_core/extension/bundle.py
src/redteam_app/composition.py
```

Implemented:

- namespaced versioned schema registrations
- relation type registration
- requirement type registration with allowed observation payload schemas
- validator registration
- evidence / observation / expectation / candidate / experiment / policy registries
- strict owner + namespace checks
- duplicate/collision failure
- bundle dependency validation + cycle rejection
- explicit application composition
- registry freeze before analysis
- canonical semantic manifest hash independent of registration order
- bundle/source implementation fingerprints included in semantic identity
- build-time explicit-file fingerprint helper; no runtime source introspection
- stale expected implementation fingerprint를 거부하는 build/test guard

## Phase 4 implementation

Primary files:

```text
src/redteam_core/investigation/requirements.py
src/redteam_core/investigation/resolution.py
src/redteam_core/knowledge/state.py
src/redteam_core/knowledge/commit.py
src/redteam_core/knowledge/recompute.py
src/redteam_core/runtime/clock.py
```

Implemented:

- stable typed `EvidenceRequirement`
- freshness contract
- 0..N resolver registration
- deterministic resolver order: trust desc -> cost asc -> stable resolver ID
- deterministic ordered fallback when a higher-ranked resolver exhausts its call budget
- required-permission filtering before resolver invocation
- per-resolver call budget accounting
- resolver side-effect-free gate
- resolution failure taxonomy
- unsupported/forbidden/invalid payload failures recorded without truth mutation
- resolver output validated against registered requirement payload schemas
- typed `ResolverOutput` carrying typed payload + validity metadata
- event-identity `Observation` with trusted provenance
- explicit `RuntimeClock` / test `FixedClock`
- freshness supports max age, required snapshot identity and explicit `not_after`
- `KnowledgeSlot`: UNKNOWN / CONSISTENT / CONFLICTING
- conflicting authoritative observations preserved with no selected winner
- inferred observation cannot override authoritative knowledge
- stale observation cannot become current truth
- duplicate exact observation is idempotent
- expected-revision CAS
- deep-copy validate/recompute then atomic ledger commit
- multi-observation commit canonicalized so completion order does not change final semantics
- explicit freshness refresh can expire previously current knowledge
- generic `SemanticRecomputer` receives a deep-copied canonical read view and returns registered Relations
- recomputer-owned relation replacement is deterministic and collision-checked
- one atomic semantic transaction performs Observation -> KnowledgeSlot -> Relation recompute -> one revision bump
- Batch A integration test proves consistent observation -> Relation TRUE, then conflicting authoritative observation -> Relation UNKNOWN

## Generic test result

Commands:

```text
uv run ruff check src tests
uv run pytest -ra
uv run python -m compileall -q src
uv build
```

Result:

```text
ruff:       All checks passed
pytest:     45 passed
compileall: PASS
build:      source distribution + wheel PASS
domain scan: PASS
donor drift/dirty check: PASS
```

Final deterministic receipts after the last Batch A code/test gate:

```text
src+tests tree SHA256:
6c8e457939fca12c10e06888ab38a21436c6f2536f9a96f416c681d7b26ab4ec

GENERIC_CORE_REFACTOR_PLAN.md SHA256:
80038e66541fa2e250052861e29637acc7f78451640f051bbb7a09f4695287d6
```

Architecture checks also verify:

- core imports none of the donor packages,
- semantic modules have no hidden `datetime.now()` calls outside the explicit clock adapter,
- generic project dependencies do not pull provider/domain stacks,
- source scan found no GitHub/Workflow/AWS/OIDC/Android/APK/ADB/JADX/account/transaction donor semantics in `src/redteam_core` or `src/redteam_app`.
- the package builds successfully as both sdist and wheel.

## Known intentionally-unimplemented work

Batch A does not yet implement:

- generic RelationSemantics / Frontier traversal,
- CandidateDeriver / root-cause CoverageLedger / multi-batch rotation,
- ArtifactBoundary and bounded acquisition implementation,
- WorkingMemory/context projection,
- validator/oracle execution runtime,
- controlled experiment/effect boundary,
- durable checkpoint/resume,
- final reference domains and composite integration proof.

Those remain Phase 5 onward. Their contracts must build on Batch A rather than bypassing its typed
registries, observation ledger, provenance, conflict, freshness and semantic revision rules.
