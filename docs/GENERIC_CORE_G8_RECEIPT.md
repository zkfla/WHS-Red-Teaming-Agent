# G8 priority and starvation control — implementation receipt

2026-09-09 · PASSED_OFFLINE · utility default enable WITHHELD_QUALITY_REGRESSION.

## Final G8 approval (supersedes intermediate status entries below)

- Exact-byte approved source:
  `921c1003cc410e8a967a3d358977570ebf07f08705c6f70a12de98509e99a09e`.
- Final same-source semantic regression: **1,118 PASS, 2 explicit prior-release
  source-approval checks deselected (459.86s)**. The hash remained unchanged.
  Ruff, compileall, 0.4.0 sdist and wheel PASS. The source checks are rerun after
  this explicit approval; they are not misreported as part of that 1,118-test run.
- `GENERIC_CORE_G8_QUALITY_AB_FINAL.json`: all **20 arms executed**, source and
  harness integrity PASS. Incorrect root decisions: zero in each arm. Utility
  regressed distinct inspected root coverage at budget 3 in uncertain-response
  and repeated-invalid-response (baseline 4, utility 3). The original gate thus
  withholds utility default enable. This is offline feature acceptance, not a
  claim of universal quality improvement or a live-provider result.
- Source/contract review covered advisory score isolation, deterministic fairness,
  fresh/conflicting evidence, physical failure counting, root identity/evidence
  separation, trusted terminal binding, checkpoint-before-publication, crash cuts,
  rollback/history preservation, current completion assessment and opaque domain
  state preservation through the real step/resolver/finalizer path.
- Runtime/package 0.4.0 and checkpoint schema 4.0 reject incompatible earlier
  checkpoints; no silent migration, replay or history/budget reset is provided.

Imports: providers and pure preparation live in `redteam_core.investigation.priority`;
durable publication lives in `redteam_core.runtime.priority.PriorityCoordinator`;
typed state lives in `redteam_core.models.priority`. Providers must be explicitly
composition-bound. Baseline priority and legacy public APIs remain available.

The release baseline advances from the historical G7 approval to this reviewed
G8 source. Historical Batch F/G7 receipts remain unchanged. G9 is a separate
local integration/default-enable gate and is not approved here. No CI/CD domain,
external model/provider, live target, commit or push was performed.

Post-approval source/reference checks: **10 PASS (1.09s)**; Ruff and diff whitespace
checks PASS. The entries below are chronological evidence, not current pending work.

This receipt records the implementation boundaries of existing plan section G8;
it does not change scope or claim implementation acceptance.

## Required implementation slices

1. Keep `score_security_candidate` and legacy portfolio API as the baseline.
   Providers receive isolated typed numeric facts and return bounded score
   components/reasons. Ranking never changes Evidence, Relation or validator sets.
2. Derive resolvability from current canonical requirements, frozen resolver
   descriptors, permissions and both durable operational/global budget limits.
   Dry-run feasibility checks must not reserve or dispatch anything. Fresh cached
   observations and conflicts are not converted to an invented success probability.
3. Persist per-root selection age, failure information, decision binding and
   assessment/selection receipts in typed runtime state. Composition must bind
   provider implementation/configuration; restart cannot reset age or silently
   accept a different policy. Explicitly composed baseline rollback keeps history.
4. Separate stable root identity from relevant evidence/expectation fingerprint.
   Relevant changes require reassessment; unrelated root changes do not reopen
   everything. Never invent the historical evidence binding of an unbound legacy
   terminal decision. MERGED and explicit user-final decisions remain locked.
5. A deterministic round-robin floor must have a provable finite stable-pool
   bound. Reject infeasible pool/slot/wait configurations explicitly rather than
   promising a bound the scheduler cannot meet. Keep family diversity in remaining
   priority slots. Admission reasons are advisory, not an execution permit.
6. Pure preparation and checkpoint-before-publication must preserve atomicity;
   recheck canonical/permission/budget/policy bindings before publishing selections.
   Storage failure poisons the live coordinator until reload.

## Acceptance still required

- Provider baseline equivalence, deterministic order, stale ref rejection,
  no truth mutation, no model/resolver calls during ranking.
- Finite stable-pool starvation bound, permission/budget blocks, failure penalty,
  fresh cache, shared roots, FALSE/conflict as valid progress.
- Root churn/relevant baseline changes/unrelated changes/MERGED/user-final cases.
- Durable checkpoint/restart and policy rollback without history reset; fault cases.
- Fixed-budget baseline/utility/shadow ablation on declared independent-ground-truth
  fixtures: solved requirements, correct root decisions, distinct root coverage,
  duplicate calls and time-to-first-valid-outcome. Report every fixture separately.
- Full regression, compatibility/version update, build and explicit source approval.

No code or gate is approved by this document. No CI/CD domain, external provider,
live target, commit or push is in scope.

## 2026-09-09 first implementation slice (not final approval)

- Added baseline and deterministic utility providers; the baseline score and
  existing portfolio API are preserved. New expected-utility ordering remains
  explicit opt-in. Numeric components include resolvability, shared unresolved
  roots, fresh evidence, conflicts, call cost, durable failure count and age.
- Typed PriorityState is owned by RuntimeState, with bounded per-root records,
  complete selection/decision history, provider binding and aggregate byte cap.
  A round-robin floor has a stated `ceil(eligible_roots / floor_slots)` bound for
  a finite stable pool; infeasible configured bounds reject instead of truncate.
- Dry-run operational/global budget checks never publish reservations. Permission
  and resolver descriptors are checked, only passive descriptors are eligible.
- Root evidence hashing includes relation/requirement dependency closure, missing
  references, expected baselines and freshness. Observation lookup is reused
  within a selection. Unrelated root evidence and candidate-ID-only churn do not
  invalidate an existing root binding. Historical unbound terminal decisions
  stay unbound and require review; MERGED/user-final records are not overwritten.
- PriorityCoordinator prepares privately, rechecks canonical inputs, permissions,
  provider configuration and freshness, then checkpoints before publication.
  Storage failures poison the coordinator; restart restores saved selection age.
  Explicitly composed baseline rollback does not erase history.
- Trusted host-only terminal decisions bind selected evidence and are idempotent.
  Neither the provider nor DecisionProposal may materialize Findings or pick
  validators. Finish assessment rechecks stale root evidence even if there was no
  intervening priority selection; a locked old decision is not current truth proof.
- Runtime/package 0.4.0, checkpoint schema 4.0; explicit old-version rejection
  tests now include 0.2.2 and G7 0.3.0. No automatic migration or budget reset.

Verification so far: **25 PASS** (priority/compatibility), **58 PASS** (expanded
priority/checkpoint/architecture), then **41 PASS (7.49s)** after finish-gate
integration (priority/completion/architecture). Ruff PASS. These focused runs
are separate and not summed into a fictional full-suite result.

Remaining: strengthen failure/cache/shared-requirement and fairness canaries,
actual fake-loop priority integration, independent fixed-budget baseline/utility
quality ablation, fault/process-restart coverage, full regression/build and source
approval. G7 remains the approved source baseline. G8 is IN_PROGRESS; G9 remains
NOT_STARTED. No performance benefit or default-enable approval is claimed yet.

## Subsequent G8 integration and quality audit

- Expanded G7/reference/priority/checkpoint/completion/architecture regression:
  **93 PASS (130.16s)**. Actual priority -> fake decision -> durable passive result
  -> host-bound terminal decision -> finish is covered. Process crash cuts before
  and after priority checkpoint publication recover exactly the published batch
  and age, without reserving or invoking a provider (**3 PASS, 14.47s**).
- Initial fixed-budget quality receipt: `GENERIC_CORE_G8_QUALITY_AB.json`, 16 arms.
  Correct terminal labels had no observed errors. Shared-evidence/budget-3 selected
  root coverage increased 3 -> 4, while the failure case/budget-3 decreased 4 -> 3.
  The predeclared per-fixture non-regression gate therefore withheld default enable.
  Coverage here means distinct selected roots whose canonical state was inspected,
  not exclusively roots that consumed an external call. Timing is one descriptive
  local sample per arm; it is not a statistical speed claim.
- The initial case named repeated-known-failure actually exercised an exception
  before a response, correctly producing UNCERTAIN and preventing further calls.
  Preserve that receipt as intermediate evidence. Fixture V2 renames it
  uncertain-response and adds an actual returned-invalid-payload case for repeated
  known failures, without changing the score weights or acceptance thresholds.
  V2 binds exact harness/core/fixture hashes and checks source integrity.
- A regression reproduced that two physical failures at the same fixed time were
  counted once due to canonical failure de-duplication. Priority now counts bound
  physical attempt identities while avoiding double counting their canonical
  failure records. The facts policy is explicitly versioned @2. Signal/fingerprint
  regression: **13 PASS (7.91s)**. The invalid-response smoke case performed 3 known
  failed dispatches, preserved failure_count=3 and made zero uncertain dispatches.
- Two preservation tests failed before fixes: an in-memory PriorityState reset
  could overwrite saved age, and omitted domain envelopes were dropped. Coordinator
  now checks durable priority/budget and checkpoint sequence before publication;
  omitted envelopes retain the current checkpoint's opaque state. **15 PASS (28.37s)**
  across preservation, durable priority, process restart and fake-loop tests.
- Extending the opaque-state canary through the real step/resolver/finish path found
  the same default-empty issue outside priority. RuntimeOrchestrator now retains
  the last explicitly checkpointed/resumed envelopes for omitted arguments, including
  effect observers and finalization; explicit empty tuples still mean an intentional
  replacement. Dispatcher defaults preserve this contract. The failing end-to-end
  case now passes; integration/preservation/checkpoint regression: **28 PASS (31.95s)**.

V2 quality run is in progress. Root-ID/candidate-ID churn audit has additionally
reproduced a stranded UNREVIEWED successor with an unchanged, already-bound root
decision; that boundary remains to be fixed before final G8 approval. G7 historical
approval is retained as its tested-stage record, not a claim that these newly found
integration defects never existed. G8 is still IN_PROGRESS and not source-approved.

## Latest source freeze for final G8 gates

- V2 completed all 20 arms with source/harness integrity PASS. Two budget-3
  coverage regressions remain: uncertain-response and repeated-invalid-response,
  each baseline 4 vs utility 3 inspected roots. Correct decisions remained 3 in
  those pairs. At budget 6 the invalid-response fixture made 3 known failed calls,
  2 duplicate calls, and retained a durable failure count of 3 in both arms.
  Default enable remains withheld; no weights or thresholds were changed to pass.
- Candidate reidentification with identical root evidence stranded the successor
  as UNREVIEWED, including a user-final predecessor. Both tests failed before
  the fix. The coordinator now privately reapplies only an existing host decision
  whose exact root evidence AND claim contract/ceiling still match, then publishes
  the reconciliation and selection atomically. It never invents a decision from
  a score, overwrites a different terminal disposition, or copies a decision onto
  changed evidence/contract. The provider itself remains advisory and read-only.
  Canonical facts/binding policy is now explicitly @3. Root churn and concurrent
  checkpoint replacement tests: **7 PASS (12.58s)**.
- Admission now includes durable receipt count/byte capacity and canonical input
  size, matching the actual passive boundary. Durable usage cannot move backwards
  relative to the checkpoint. Root churn/preservation/signals/fingerprint regression:
  **21 PASS (20.12s)**; Ruff, compileall, sdist/wheel for 0.4.0 PASS.
- Current frozen candidate source:
  `921c1003cc410e8a967a3d358977570ebf07f08705c6f70a12de98509e99a09e`.
  Final same-source quality receipt (`GENERIC_CORE_G8_QUALITY_AB_FINAL.json`) and
  full semantic regression will run sequentially, so their timing is not affected
  by running each other concurrently. Both remain pending at this entry. Prior
  quality receipts retain their own source hashes and are not relabeled as final.

G8 is not yet source-approved; the release baseline remains the G7 hash. G9 and
the plan-wide final acceptance audit remain pending. No live/provider/domain
integration, commit, push or default-enable change has been performed.
