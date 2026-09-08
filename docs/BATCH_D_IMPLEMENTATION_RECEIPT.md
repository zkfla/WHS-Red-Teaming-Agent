# Batch D Implementation Receipt — Phase 9~10

Date: 2026-09-08

## Result

`COMPLETE` for Batch D as defined in `GENERIC_CORE_REFACTOR_PLAN.md`, subject to the final deterministic
hash fields at the end of this receipt.

Batch D implements Phase 9 (Validation / Oracle subsystem) and Phase 10 (Controlled Experiment /
ExecutionPolicy / EffectBoundary / retry + cleanup lifecycle). It does **not** claim Phase 11 durable
checkpoint/resume or final heterogeneous reference/composite proof is implemented.

## Locked donor commits

| Donor | Commit | Final status |
|---|---|---|
| CI/CD | `ee89e3063dc5f25fb19af950e4a3e4023dbd6d5d` | clean |
| Financial | `83885a478b9151ab4be652caff6ba564dce8ee6e` | clean |
| Mobile | `f1ed305fb384de7e0043df8fc5e01f4935ab4689` | clean |

No donor source file was modified by Batch D.

## Phase 9 — Validation / Oracle

Primary implementation:

```text
src/redteam_core/models/validation.py
src/redteam_core/models/security.py
src/redteam_core/validation/engine.py
src/redteam_core/validation/__init__.py
src/redteam_core/extension/registry.py
src/redteam_core/extension/bundle.py
tests/unit/test_validation_engine.py
```

Implemented invariants:

- Validator applicability is frozen into `ValidatorRegistration` and the semantic manifest.
- runtime validator set and applicability must exactly match the frozen manifest.
- Candidate has no validator allowlist field; LLM/Candidate cannot shrink system-owned validation.
- required runtime result absence yields an explicit validator-specific `UNKNOWN` outcome.
- validator exception and zero-output also preserve an `UNKNOWN` audit slot instead of disappearing.
- validator receives deep session/candidate/result snapshots, not authoritative mutable state objects.
- supplied ExperimentResult must exist exactly in the canonical execution result ledger.
- validator evidence refs outside the canonical candidate/relation/result context fail closed to UNKNOWN.
- every PASS/UNKNOWN/ANOMALY/VIOLATION outcome is durably preserved.
- deterministic Finding materialization creates 0..N Findings while retaining source outcome refs.
- validation engine imports no LLM/network/subprocess client and cannot invoke the execution effect path.

## Phase 10 — Controlled Experiment / Policy

Primary implementation:

```text
src/redteam_core/models/execution.py
src/redteam_core/execution/policy.py
src/redteam_core/execution/runtime.py
src/redteam_core/execution/cleanup.py
src/redteam_core/execution/__init__.py
src/redteam_core/models/provenance.py
src/redteam_core/extension/registry.py
src/redteam_core/extension/bundle.py
tests/unit/test_execution_policy.py
```

Implemented invariants:

- typed registered experiment action/result/policy-constraint payloads; no canonical free-form Any bag,
- immutable `TypedPayloadEnvelope`, including policy constraint envelopes,
- manifest-bound ExperimentExecutor implementation/action/result applicability,
- manifest-bound PolicyConstraintEvaluator implementation/constraint applicability,
- deterministic immutable `ExecutionPolicySnapshot` with effective hash, approval provenance and
  semantic-manifest binding,
- one explicit active policy snapshot; policy replacement invalidates use of old authorization,
- semantic `experiment_id` separated from operational sequential `attempt_id`,
- `PLANNED → AUTHORIZED → STARTED → SUCCEEDED|FAILED|UNCERTAIN` attempt lifecycle,
- `ExecutionPermit` binds attempt/descriptor/policy/manifest/effect/capabilities/expiry,
- final boundary rechecks stop state, scope, action, effect, capability, concurrency, total started
  attempt budget, cleanup requirement and all typed constraints before invoking executor,
- HIGH_IMPACT action requires explicit descriptor capability and policy capability approval,
- STARTED executor/result-validation exception becomes UNCERTAIN rather than invented FAILED truth,
- automatic retry defaults closed and requires explicit idempotency/reconciliation semantics plus an
  operational key; manifest changes block retry,
- created resources remain tracked when post-effect result validation fails,
- CreatedResource→CleanupRequirement→CleanupAttempt→CleanupOutcome durable accounting,
- absent cleanup implementation is explicit terminal `NOT_CONFIGURED`.

## Batch D integration proof

`tests/integration/test_batch_d_spine.py` exercises:

```text
canonical SecurityCandidate
→ typed ExperimentDescriptor
→ immutable active ExecutionPolicySnapshot
→ policy authorization + ExecutionPermit
→ designated ExperimentExecutor effect boundary
→ canonical ExperimentResult
→ system-owned Validator registry
→ ValidationOutcome
→ Finding with source-outcome/evidence grounding
```

## Generic release gates

```text
uv run ruff check src tests                  PASS
uv run pytest -ra -o addopts=''              137 passed
uv run python -m compileall -q src           PASS
uv build                                     PASS
architecture/domain-boundary tests           PASS
```

## Donor conformance regression

CI/CD locked donor focused suites:

```text
tests/unit/test_validator.py
tests/unit/test_security_candidates.py
tests/unit/test_proposal_draft.py

19 passed
```

Financial locked donor focused suites were run using `uv run --no-project` with ephemeral dependencies
so the donor repository was not mutated:

```text
tests/test_oracles.py
tests/test_execute_spine.py
tests/test_engagement_policy.py
tests/test_policy_enforcement.py
tests/test_cleanup_tracking.py

68 passed
```

## Intentionally still incomplete

- Phase 11 durable checkpoint/resume/event journal/finalizer,
- final reference_static_graph/stateful_experiment/artifact_runtime/request_response packages and
  composite integration proof,
- full trusted Expectation/Baseline ledger consumption by validators (`F-01` remains partial),
- concrete Mobile runtime adapter (`M-06` remains partial),
- untrusted third-party Python plugin sandboxing (outside current trust model).

## Deterministic receipts

Filled after final document synchronization and release gates:

```text
src+tests tree SHA256: d9cc165d67fe4d4b5c9912dcf80ea1dce75fd25a1f8d2af4af3a371497303bd7
GENERIC_CORE_REFACTOR_PLAN.md SHA256: 0fc35ed502863efae68bdafe028b325ed4ceac23f8b7d62e3893ee953ab3d011
```
