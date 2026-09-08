# G7 generic step loop — implementation progress

2026-09-09 · PASSED_OFFLINE · provider-free only.

## Final G7 approval (supersedes intermediate status entries below)

Current exact-byte source:
`c658b767533dfb57f304de23ca1ac5a5a97d8c83877af20fc2e30284c185f88b`.
Final current-source semantic regression: **1,077 PASS, 2 explicit prior-release
source approval checks deselected (462.67s)**. The source hash was unchanged at
completion. Ruff, compileall, sdist/wheel for 0.3.0 passed. Checkpoint schema 3.0
and composition/schema hashes reject incompatible old resume rather than migrate
or reset budget/progress. The contract-to-evidence map below covers the G7 gates.

After current-source semantic tests and source/contract review, the release
baseline is explicitly advanced from G6 to G7. Historical Batch F/G6 receipts
remain intact. The two approval checks and reference tests are rerun separately;
their result is recorded below, not invented as a single 1,079-test run.

Post-approval source/reference checks: **10 PASS (1.22s)**; Ruff and diff whitespace
checks PASS. G7 is PASSED_OFFLINE. Subsequent G8 source changes require new approval.

The API is explicitly composed and opt-in. No external model SDK, real target,
domain integration, validator selection or effect-boundary bypass was added.
G7 completion does not approve G8/G9 or prove live-agent performance. Prior
IN_PROGRESS sections are chronological development evidence, not current status.

## Implemented initial slice

- Typed DecisionProposal, DecisionDriverDescriptor, ProgressPolicy/State and StepReceipt.
- RuntimeState typed progress ledger and composition binding for driver/context/progress policy.
- Bounded fake decision requests, operational reservation, malformed repair (maximum one), revision/ref checks.
- Semantic no-op, cosmetic text edit, ping-pong and bounded waiting tests.
- CompletionAssessment keeps scope, unresolved requirements, blocked roots, uncertain effects and cleanup gaps separate from finalization.
- Optional existing passive scheduler integration; no direct executor or validator selection API.
- Four reference-domain finish-request smoke cases pass. These are not complete investigation-loop acceptance.

## Evidence and remaining gates

Initial step/completion/reference suite: 16 PASS before later token/finalizer additions. Expanded step/reference/completion/checkpoint/dispatcher/scheduler regression: **102 PASS**. Ruff and compileall PASS. This is a focused regression result, not final G7 full-suite or recovery acceptance.

Mandatory remaining work before G7 can pass:

1. Persist typed decision response/proposal and recovery phase atomically; handle crash after response but before step receipt without replaying a consumed attempt.
2. Poison engine after storage failures; do not convert internal errors or cancellation indiscriminately into ordinary WAITING. Enforce single writer/reentrancy and stopped runtime at every step boundary.
3. Enforce combined runtime and operational budgets, preserve unknown/overrun token usage, count repair feedback and schema reserve before dispatch.
4. Finish G6 incomplete-context retrieval and experiment-plan request contracts; placeholders returning WAITING alone are not finished actions.
5. Strengthen progress fingerprints: exclude revision-only/duplicate observation changes, invalidate on permissions/freshness/evidence changes, bound receipt capacity before dispatch, make STOP terminal.
6. Persist scope/completion assessment and bind finish policy priority to the finalizer; add full reference investigation action paths and fault matrix.
7. Bump checkpoint/runtime/package compatibility for G7, run full regression/build, and approve a new source hash only after all gates pass.

The approved source baseline remains G6. Current G7 source intentionally differs from that baseline. G8/G9 remain NOT_STARTED. No commit/push or real domain/provider execution occurred.

## 2026-09-09 resume audit (authoritative current status)

G7 remains **IN_PROGRESS**, not release approved. The pre-pause full semantic
regression finished successfully with the two explicit source-approval gates
deselected. That run predates the following changes and does not approve them.

- Validated provider token overruns were discarded by the rejected-response path.
  A new regression failed before the fix (`REJECTED` instead of `STOP_PARTIAL`).
  The fix preserves actual usage, including 9,600 tokens beyond the reservation,
  durably records the bound violation, and prevents a repair request. Malformed
  envelopes still keep unknown usage conservatively reserved. Both tests pass.
- Historical context retrieval content is rebuilt from its canonical reference
  instead of being trusted as current state. A forged/stale retrieval regression
  confirms the current FALSE relation replaces supplied stale content.
- Focused context plus token accounting tests: **11 PASS (7.50s)**.
- The response, step, retrieval and completion ledgers and package/runtime 0.3.0,
  checkpoint schema 3.0 are implemented, but final recovery audit, full regression,
  build and source approval remain required. G8/G9 have not started.

- Completed-resolution recovery previously bypassed changed authority epochs when
  accepting the historical response. The new test failed before the fix. A separate
  `control_fingerprint` in the durable response now binds instruction/authority
  epochs, scope, active portfolio and the current prebound experiment descriptors.
  Recovery refuses changed controls while unchanged controls reuse the stored
  result without another model/resolver call. Both response-recovery tests pass.
- Intermediate G7 step/reference/context/completion/token suite: **45 PASS
  (136.11s)** before the control-fingerprint change. Do not treat this as the
  final current-tree full-suite receipt.

Remaining release audit includes retrieval/response capacity boundaries, actual
process-restart recovery coverage and current-tree full regression.
The historical G6 approved source hash is unchanged.

Current resume slice verification: **83 PASS (149.99s)** across step, four reference
domains, token accounting, operational/checkpoint recovery, focused context,
semantic progress and completion tests. Ruff, compileall and git diff --check PASS.
sdist and wheel build PASS for 0.3.0. This is focused regression plus packaging,
not the G7 final source approval or G8/G9 acceptance. No commit/push performed.

### Subsequent G7 boundary/restart slice

- Response receipt metadata is included in the byte-bound validation before
  settling success. Oversized receipt metadata now produces a durable FAILED
  reservation with known usage, instead of leaving a seen reply as dispatched.
- Query candidate/ref mismatches and new retrievals beyond the 64-entry ledger
  capacity are rejected before retrieval. Existing refs may be refreshed.
- Retrieval budgets reject invalid/bool/out-of-range values. Even an incomplete
  marker must fit the caller's character and UTF-8 limits or fail closed.
- Boundary/context/token tests: **17 PASS (6.47s)**.
- Actual subprocess crash cuts after dispatch and after durable response:
  **2 PASS (7.49s)**. Workers terminate with os._exit; fresh parent orchestrators
  load their checkpoint/journal. No replacement driver calls occur. Unknown
  dispatch remains UNCERTAIN; saved response produces the finish assessment.
- Ruff and compileall PASS. This boundary/restart source completed full semantic
  regression: **1,063 PASS, 2 source-approval gates deselected (310.76s)**.

### No-progress audit after that full regression

The next three tests all failed before the fix: repeated REJECTED, ERROR and
NO_PROGRESS outcomes escaped the replan/stop threshold when the full context
fingerprint changed without truth-relevant change. Error/rejection outcomes now
participate in the same bounded detection, and action keys bind semantic input
plus controls instead of cosmetic context revisions. Full input fingerprints
remain in response/step receipts for replay validation. Focused step and stuck
regression: **23 PASS (66.30s)**, Ruff PASS. The 1,063-test result predates this
change and is not its final approval. G7 remains IN_PROGRESS; next is the remaining
G7 contract audit and current-tree full gate, then G8/G9 in the original order.

### Completion/context contract audit

- Nonterminal experiment attempts are now explicit `pending_experiment_ids` and
  force PARTIAL. Execution policy stop also prevents a COMPLETE assessment.
  Authorized and STARTED experiment cases are covered alongside existing
  uncertain-effect/cleanup cases (completion suite: 6 PASS).
- Focused context now exposes working-memory revision required by the proposal
  contract. Token measurement is valid without an optional total-token ceiling;
  the step engine passes its tokenizer when a focused total-token budget is set.
- Focused context/completion/stuck regression: **24 PASS (3.93s)**.

G7 is not yet release approved. Remaining contract audit must cover freshness
expiry during a running loop (not just changed canonical observations), semantic
recompute wiring after passive resolution, and non-noop replan triggers specified
in plan G7. These are existing plan requirements, not a new domain integration.
Latest aggregate step/reference/restart/checkpoint/context/completion regression:
**81 PASS (114.37s)**. This focused result does not substitute for the remaining
contract work or the current-tree full release gate.

### Semantic pipeline and in-loop expiry slice

- GenericStepEngine accepts a bounded unique set of existing SemanticRecomputer
  implementations. A nonempty set requires an explicit versioned pipeline hash;
  the hash is bound in runtime composition/checkpoint compatibility and preserved
  by durable resolver dispatch. The passive commit invokes these recomputers.
- Before constructing the next decision context, expired observations in current
  knowledge slots trigger private freshness recomputation and durable publication.
  Dependent Relations must have their configured owner recomputer; otherwise the
  engine refuses to proceed rather than exposing a stale TRUE verdict.
- The reference runtime action now proves UNKNOWN -> TRUE after its fake
  observation, and TRUE -> UNKNOWN after clock-driven expiry. Finish then remains
  PARTIAL with UNRESOLVED_REQUIREMENTS. All **8 reference loop tests PASS (26.00s)**.
- Initial ownership-collision failure was corrected in the reference fixture by
  declaring the initial Relation's actual recomputer owner. Collision protection
  in core was not weakened.
- Expanded checkpoint/dispatcher/step/process-restart/reference regression:
  **82 PASS (133.04s)**; Ruff and compileall PASS.
  Non-noop replan event triggers and final full-source approval remain
  pending; G7 is still IN_PROGRESS, G8/G9 remain NOT_STARTED.

### Canonical event replan slice — 2026-09-09

- Added typed, bounded durable ReplanEvaluation entries for new evidence,
  dependency conflicts, blocked roots and a versioned remaining-budget-drop
  threshold. Unchanged state does not retrigger an event. These are advisory
  hints for the next ordinary decision request, never extra provider calls,
  truth mutations or execution authority. No-progress retains its existing
  bounded replan/stop policy.
- Event detection, durable checkpoint round-trip, process restart and stuck
  regression: **10 PASS (15.52s)**. Existing generic step integration:
  **20 PASS (75.95s)**. Ruff PASS.
- The first added checkpoint test used the wrong loader keyword/attribute;
  corrected the test to the existing composition_fingerprint and journal API.
  No checkpoint contract was weakened to make it pass.
- Current-tree full semantic regression is running with only the two explicit
  release-source approval checks excluded. G6 remains the approved baseline
  until full verification and G7 source review complete. G7 is IN_PROGRESS.

### Explicit progress-policy recovery audit

Two new regressions failed before the fix: explicitly passing the same policy
when reconstructing an in-flight first-step engine reset its active attempt and
event history; passing an incompatible policy mutated runtime before composition
validation rejected it. Initialization now preserves matching durable state and
checks a proposed policy before publishing it. Only genuinely pristine progress
can adopt a different composition-bound policy.

Policy recovery, event and actual process-restart regression: **9 PASS (13.59s)**;
Ruff PASS. The preceding full run was deliberately terminated before completion
because this new fix required a fresh run; it is neither PASS nor test failure.
A new full semantic run began after the source fix. No G7 approval yet.

## G7 contract-to-evidence map

| Contract | Executable evidence |
|---|---|
| Typed bounded proposal, refs/revisions, repair limit | `test_generic_step_loop.py` wrong-revision/forged-evidence and malformed-budget cases |
| Operational/global/round/token limits | `test_generic_step_loop.py`, `test_decision_token_accounting.py` |
| No-op, wording churn, repeated errors, ping-pong, bounded waiting | `test_generic_step_loop.py`, `test_decision_stuck_boundaries.py`, `test_semantic_progress_fingerprint.py` |
| Event-only advisory replan and durable evaluation | `test_replan_events.py` |
| Bounded canonical retrieval and trusted experiment planning only | `test_focused_context.py`, `test_generic_step_loop.py` |
| Finish is not proof of completeness | `test_completion_assessment.py`, `test_g7_reference_step_loop.py` |
| Response replay, unknown dispatch, authority binding and storage poison | `test_generic_step_loop.py`, `test_decision_process_restart.py` |
| Explicit policy reinitialization cannot erase in-flight progress | `test_progress_policy_recovery.py` |
| Four provider-free reference action paths, recompute and expiry | `test_g7_reference_step_loop.py` |

This map identifies tests, not a replacement for the still-pending current-tree
full regression/source approval. No live LLM or real target acceptance is claimed.

### Latest full semantic regression and decision instrumentation

- Post-policy-fix source `5444799378bd2c188fa33b1d4294b91a0042766470913ae399e4ef61e5c5d1a3`:
  **1,076 PASS, 2 explicit source-approval gates deselected (404.52s)**.
  Focused diagnostic tests ran alongside parts of this regression; elapsed
  time is not a benchmark or performance comparison.
- G0's deferred decision span was still unconnected. The real-engine test
  reproduced zero DECISION events before the fix. `run_step` now uses the
  existing opt-in performance decorator, counting active portfolio items.
  Measurements contain numeric metadata only; input_bytes remains explicitly
  unavailable rather than serializing raw payloads for instrumentation.
- Disabled/enabled/failing observer arms produce identical step and canonical
  session output. Decision telemetry, generic observer isolation/cancellation
  and policy recovery: **12 PASS (14.15s)**; Ruff, compileall, sdist and wheel PASS.
- Latest source is
  `c658b767533dfb57f304de23ca1ac5a5a97d8c83877af20fc2e30284c185f88b`.
  The 1,076-test run precedes the decorator connection. A final current-source
  full semantic regression is required before approving G7; G6 baseline stays
  unchanged. G8/G9 remain NOT_STARTED. No commit/push or external execution.
