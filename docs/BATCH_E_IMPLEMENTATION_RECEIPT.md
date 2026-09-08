# Batch E Implementation Receipt — Phase 11

Date: 2026-09-08

## Result

`COMPLETE` for Batch E / Phase 11 as defined in `GENERIC_CORE_REFACTOR_PLAN.md`.

Batch E implements the durable Runtime / Checkpoint layer. It does **not** claim Phase 12 reference
domains, Phase 13 composite proof, Phase 14 final conformance closure, full trusted baseline
consumption (`F-01`) or a concrete Mobile runtime adapter (`M-06`) are complete.

## Locked donor commits

| Donor | Commit | Final status |
|---|---|---|
| CI/CD | `ee89e3063dc5f25fb19af950e4a3e4023dbd6d5d` | clean |
| Financial | `83885a478b9151ab4be652caff6ba564dce8ee6e` | clean |
| Mobile | `f1ed305fb384de7e0043df8fc5e01f4935ab4689` | clean |

No donor source file was modified by Batch E.

## Primary implementation

```text
src/redteam_core/models/runtime.py
src/redteam_core/models/domain_state.py
src/redteam_core/models/execution.py
src/redteam_core/runtime/checkpoint.py
src/redteam_core/runtime/journal.py
src/redteam_core/runtime/domain_state.py
src/redteam_core/runtime/budget.py
src/redteam_core/runtime/termination.py
src/redteam_core/runtime/finalizer.py
src/redteam_core/runtime/lease.py
src/redteam_core/runtime/orchestrator.py
src/redteam_core/execution/runtime.py
src/redteam_core/execution/cleanup.py
src/redteam_core/investigation/resolution.py
src/redteam_core/extension/registry.py
src/redteam_core/extension/bundle.py
tests/unit/test_runtime_checkpoint.py
tests/integration/test_batch_e_spine.py
tests/architecture/test_core_boundaries.py
```

## Implemented invariants

- checkpoint writes use a flushed/fsync'd temporary file plus atomic replacement,
- an existing checkpoint is fully validated before replacement, so corruption cannot be silently
  erased by the next successful write,
- the runtime journal is append-only, fsync'd and hash-chained with contiguous sequence numbers,
- only a syntactically torn final journal line may be repaired; semantic tampering or earlier
  corruption fails closed,
- checkpoint compatibility binds core schema/runtime version, frozen semantic manifest, resolver
  descriptor set, resolver-routing policy and priority-provider identity,
- runtime composition mismatch rejects resume unless a future explicit migrator is added,
- `DomainStateCodecRegistration` participates in the semantic manifest and runtime codec sets must
  exactly match that manifest,
- `DomainStateEnvelope` persists one opaque canonical payload per bundle with schema/codec/hash
  binding; checkpoint code never interprets domain payload keys,
- an OS advisory `RunLease` prevents two processes from writing one runtime concurrently,
- experiment external effects persist STARTED state before a durable start marker and before the
  executor call,
- effect-start or cleanup-start ambiguity after interruption becomes `UNCERTAIN`, never invented
  success/failure,
- terminal experiment/cleanup state is checkpointed before its terminal journal marker,
- automatic retry still obeys Phase 10 idempotency/reconciliation safety and never follows merely
  from resume ambiguity,
- runtime usage counters are monotonic,
- termination authority is deterministic and policy/budget stop outrank LLM finish,
- normal completion, budget stop, policy stop, LLM finish, handled external failure and internal
  error all use the same finalizer,
- FINALIZING and cleanup STARTED/terminal state are checkpointed, and cleanup failure/uncertainty/
  missing implementation stays explicit,
- a FINALIZING resume exposes `requires_finalization=True` so orchestration cannot silently continue
  the ordinary investigation loop,
- the final FINALIZED checkpoint is durable before the `RUN_FINALIZED` event marker.

## Crash-cut matrix proof

| Cut | Expected recovery | Proof |
|---|---|---|
| Observation before semantic commit checkpoint | resume older revision, reapply once | Batch E integration |
| Observation after semantic commit checkpoint | duplicate observation is semantic no-op | Batch E integration |
| Candidate/Coverage before checkpoint | resume previous candidate/coverage revision | Batch E integration |
| Candidate/Coverage after checkpoint | committed batch state persists exactly | Batch E integration |
| Experiment STARTED before external completion | attempt becomes `UNCERTAIN` | runtime checkpoint tests |
| Experiment effect completed in memory but terminal checkpoint missing | persisted STARTED state recovers `UNCERTAIN` | terminal-crash observer test |
| cleanup STARTED before completion | cleanup attempt/outcome becomes `UNCERTAIN` | cleanup recovery tests |
| finalizer entered but not completed | resume returns `requires_finalization=True` | finalizer re-entry test |

No crash-cut proof treats timeout/interruption as affirmative success or safe failure.

## Domain-state proof

The reference test codec proves:

```text
bundle-owned typed state
→ DomainStateCodec.encode
→ opaque canonical DomainStateEnvelope
→ checkpoint
→ resume
→ DomainStateCodec.validate/decode
→ original typed state
```

Changing the codec implementation fingerprint changes the semantic manifest, a missing runtime codec
fails closed, duplicate bundle-state entries are rejected, and an architecture test prevents
`runtime/checkpoint.py` from reading `canonical_payload_json` or calling `json.loads()` on domain
payloads.

## Generic release gates

Latest-code gates after all Batch E source changes:

```text
uv run pytest -ra -o addopts=''       165 passed in 10.94s
uv run ruff check src tests           PASS
uv run python -m compileall -q src tests
                                      PASS
uv build                              PASS
sdist                                 redteam_core-0.1.0.tar.gz
wheel                                 redteam_core-0.1.0-py3-none-any.whl
```

Crash/runtime determinism was additionally checked under three Python hash seeds:

```text
PYTHONHASHSEED=1     26 passed
PYTHONHASHSEED=7     26 passed
PYTHONHASHSEED=777   26 passed
```

## Donor conformance regression

CI/CD locked donor:

```text
tests/unit/test_runtime_support.py
tests/integration/test_resume_analysis.py

15 passed
```

Financial locked donor was executed with `uv run --no-project` and ephemeral dependencies, avoiding
donor mutation:

```text
tests/test_oracles.py
tests/test_execute_spine.py
tests/test_engagement_policy.py
tests/test_policy_enforcement.py
tests/test_termination_policy_stop.py

69 passed in 22.25s
```

The Mobile donor has no Phase-11-specific runtime persistence implementation to regress. Its locked
HEAD and clean state were nevertheless rechecked together with the other two donors.

## Intentionally still incomplete

- Phase 12 four heterogeneous reference domains,
- Phase 13 composite multi-domain proof,
- Phase 14 final source-conformance closure,
- full trusted Expectation/Baseline ledger consumption by validators (`F-01` remains partial),
- concrete Mobile runtime adapter (`M-06` remains partial),
- untrusted third-party Python plugin sandboxing (outside the current trusted-bundle threat model).

## Deterministic receipts

Filled after final document synchronization and final source/test hash pass:

```text
src+tests tree SHA256: 847bac94a826e558f9d47458db0f5477c180e8ba3bf1bb1f8c6b491b5f187adb
GENERIC_CORE_REFACTOR_PLAN.md SHA256: 68a26d4202f7e490f407e891c13333863fe42100716c3253c4ce5425e5f46d2e
```

The tree digest covers regular files under `src/` and `tests/`, sorted by POSIX-style relative path,
hashing `relative-path + NUL + exact-file-bytes + NUL`; `__pycache__` and `.pyc` build caches are
excluded.
