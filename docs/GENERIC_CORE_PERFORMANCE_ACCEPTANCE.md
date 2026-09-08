# Generic Core performance evolution — acceptance audit

2026-09-09 · **PASSED_OFFLINE / FINAL ACCEPTANCE**.

Authoritative scope: `GENERIC_CORE_PERFORMANCE_EVOLUTION_PLAN.md`, including
its numbered implementation items, common invariants and revised release rules.
This document does not narrow that scope. G0-G4/G6-G9 are accepted offline;
G5 and C1-C4 are conditionally deferred exactly as the plan permits. A planning
score of 100/100 is not an implementation or quality score.

## Approved source and current evidence ceiling

- Historical Batch F commit/source are preserved in Git objects and
  `tests/baselines/generic_core_release.json`.
- Approved core: G9,
  `7293c3e3ca79ba3177f27ee5a52c48c41719e7573996f1325a522b54b9945ce5`.
- Final approved-tree regression: all 1,166 collected tests ran to 100% with exit
  0 and no failure/skip/deselection markers. The earlier 1,164 semantic run plus
  10-test post-approval gate preserves the approval sequence. Ruff, compileall,
  0.4.1 sdist/wheel and diff checks passed.
- G9 added 18 composed functional tests, four benchmark-harness tests and a
  final 105-execution/three-allocation isolated composition receipt. All passed.
- No external LLM, provider SDK, real target, actual domain implementation,
  automated CI service, commit or push is included. LIVE_LLM_VALIDATED = NOT_RUN.

## Numbered implementation contract audit

Paths in this table are relative to `tests/`; production boundaries are in the
linked stage receipts. PASS below means the named stage's tested offline
contract, not approval to enable every optimization by default.

| Plan items | Concrete executable evidence | Current disposition |
|---|---|---|
| G0 metric definitions, null/units, disabled/enabled/error isolation | `performance/test_runtime_telemetry.py`, `performance/test_telemetry.py`, `integration/test_decision_telemetry.py` | PASS; input_bytes is explicitly unavailable, not 0 |
| G0 scales, seed, growth 1/10/100, sampled watchdog and separate allocation timing | `performance/workloads.py`, `performance/test_statistics_workloads.py`, `performance/test_runner.py`, G0/G1/G2 raw receipts | PASS tooling; reported large-case TIMEOUT remains unproven |
| G0 independent Knowledge/graph/transition/completion/scheduler oracle | `performance/oracles.py`, `test_oracles.py`, `test_frontier_dominance.py`, `test_scheduler_oracle.py`, `test_passive_scheduler_oracle.py` | PASS; G0-C1 counterexample and separate correctness receipt preserved |
| G1.1 requirement-scoped lookup and rebuild | `performance/test_observation_index.py` scan-count/rebuild/differential cases | PASS; operation-local, not long-lived cache |
| G1.2 authoritative ID lookup and deterministic tie-break | `performance/test_observation_index.py`, `unit/test_knowledge_resolution.py` | PASS; default decision withheld below |
| G1.3 unknown/duplicate/conflict/inferred/stale/collision | `performance/test_observation_index.py`, independent Knowledge oracle | PASS |
| G1.4 public mutation, no stale persistent index | `test_index_is_rebuilt_after_public_ledger_mutation` and fresh-operation differential | PASS |
| G2.1 search-local positive/negative sink memo | `performance/test_frontier_memo.py` | PASS; overflow may repeat BFS, within explicit cap |
| G2.2 charged stages, legacy expansion semantics | `performance/test_frontier_work_budget.py` | PASS; work units are logical visits, not CPU instructions |
| G2.3 query v2, limits and partial/stop metadata | `test_work_limit_never_overshoots_or_reports_incomplete_as_complete`, legacy/v2 oracle | PASS |
| G2.4 initial sources, zero expansions, guard/queue/fanout/aux limits | `performance/test_frontier_work_budget.py` | PASS |
| G2.5 structural hint ceiling, guard/context/dominance | `performance/test_frontier_dominance.py`, `unit/test_generic_frontier.py` | PASS; no authorization proof inferred from sink hints |
| G3.1 durable global/provider/resolver/concurrency/token accounting | `unit/test_operational_budget.py`, `integration/test_operational_budget_checkpoint.py` | PASS |
| G3.2 reserve -> durable intent -> invocation -> reconcile | `integration/test_durable_resolver_dispatch.py` and checkpoint fault cases | PASS; counts adapter invocations, not hidden transport calls |
| G3.3 only proven-unsent release; uncertain charged; new retry attempts | dispatcher and operational-budget crash-cut tests | PASS |
| G3.4 token upper-bound admission and actual overrun retention | dispatcher tests, `integration/test_decision_token_accounting.py` | PASS; unsupported hard bounds fail closed |
| G3.5 all fake decision/repair calls charged | `integration/test_generic_step_loop.py`, decision token accounting | PASS; no real SDK adapter |
| G4.1 frozen admission, passive execution separated from atomic commit | `integration/test_resolution_receipts.py`, `test_passive_scheduler.py` | PASS |
| G4.2 permissions/auth/qualifier/snapshot/descriptor-sensitive sharing | passive scheduler plus `performance/test_passive_scheduler_oracle.py` | PASS |
| G4.3 serial default, 2/4/8 worker bounds, deadlines/cancellation | `test_bounded_workers_can_run_profiles_without_exceeding_limit`, shutdown/deadline tests | PASS; concurrency speed default withheld |
| G4.4 one invocation for shared waiters, structured failures/provenance | `test_duplicate_waiters_share_one_call_and_one_observation`, cancelled waiter/receipt tests | PASS |
| G4.5 durable response before atomic ordered commit, real clock retained | receipt recovery tests; G9 full-object serial/concurrent comparison | PASS |
| G4.6 explicit ordered fallback and separate attempt budget | `integration/test_passive_fallback.py` | PASS; known invalid-payload failures only, no uncertain auto-replay |
| G5.1-5 StateDelta/read dependencies/topological ordering/isolated incremental replace | Conditional trigger review below | DEFERRED; not implemented or claimed |
| G6.1 support/contradiction/guard/requirement/Expectation bundles | `unit/test_focused_context.py`, `performance/g6_context_benchmark.py` | PASS |
| G6.2 active ID selection without full global row serialization | focused-context unrelated-row/complete-bundle tests | PASS |
| G6.3 trusted policy/objective channels; byte/character/token distinction | focused-context Unicode/token-budget tests | PASS; synthetic token counts are explicitly estimates |
| G6.4 whole bundle or explicit incomplete/retrieval | missing-reference and incomplete marker tests; generic step retrieval tests | PASS |
| G6.5 protocol groups/schema/output reserve; no prompt-cache claim | `unit/test_context_projection.py`, `test_focused_context.py` | PASS |
| G6.6 deterministic omission/retrieval; no raw payload or LLM summary | focused-context raw-payload/historical-retrieval canaries | PASS |
| G7.1 typed fake driver and canonical proposal validation | `integration/test_generic_step_loop.py` | PASS |
| G7.2 bounded allowed actions, trusted experiment planning only | `test_experiment_request_can_only_plan_a_trusted_prebound_descriptor` | PASS; no validator allowlist/action bypass |
| G7.3 semantic no-progress independent of cosmetic changes | `unit/test_semantic_progress_fingerprint.py`, sentence/Observation churn tests | PASS |
| G7.4 no-op/errors/ping-pong, one replan then partial stop | `integration/test_decision_stuck_boundaries.py`, generic step tests | PASS |
| G7.5 bounded normal waiting and dependency/control invalidation | stuck-boundary, replan and progress-policy recovery tests | PASS |
| G7.6 event-triggered replan, max-one malformed repair with budget | `test_replan_events.py`, malformed/wrong-revision/forged-evidence cases | PASS |
| G7.7 separate finish request, completion assessment and finalization | `unit/test_completion_assessment.py`, step/opaque-state finalizer integration | PASS |
| G7.8 empty scope and blocked-only roots do not imply clean/complete | completion truth table and four reference-domain step tests | PASS |
| G8.1 advisory provider and legacy score preservation | `unit/test_priority.py`, `integration/test_durable_priority.py` | PASS |
| G8.2 deterministic opportunity/cost/failure/aging facts, no invented probability | `integration/test_priority_signals.py` | PASS; physical failures counted separately from canonical deduplication |
| G8.3 feasibility, root/family diversity, finite stable-pool fairness | unit fairness/admission tests, durable restart preservation | PASS; infeasible fairness bound rejects explicitly |
| G8.4 root evidence/baseline binding, churn and locked decisions | `test_priority_root_churn.py`, `test_priority_preservation.py`, stale finish cases | PASS; no score-derived terminal judgments |
| G8 fixed-budget quality and rollback gate | `GENERIC_CORE_G8_QUALITY_AB_FINAL.json`, baseline rollback/process-restart tests | PASS evaluation; utility default enable withheld for two coverage regressions |
| G9 all toggle combinations, cold/warm/new-runtime resume, serial/concurrent | `integration/test_g9_composition.py` | PASS; 18 tests/48 composed executions plus final full regression |
| G9 current-source individual and combined timing/memory/default decisions | G9 raw receipts and final receipt | PASS evaluation; no overall speedup established, all optional defaults withheld |

## Common invariants and revised gates

| Requirement | Evidence and remaining boundary |
|---|---|
| Domain-independent core; no concrete target/provider types | `architecture/test_core_boundaries.py`, frozen bundle/reference composition and final semantic rerun PASS |
| Candidate before Finding, optional paths, all applicable validators | original candidate/validation unit and Batch B/D/F tests retained |
| UNKNOWN/conflict/stale/truncation and normative baseline stay distinct | independent oracles, reference domains, work-budget/CompletionAssessment tests |
| CAS, deep-copy isolation, single writer, retained fsync order | knowledge atomicity, checkpoint journal/lease/corruption, dispatcher and actual process-crash cuts |
| Effect permit/final boundary, UNCERTAIN recovery, cleanup | original execution-policy and Batch D/E tests retained; no passive scheduler route to effect executor |
| Raw retention/export/LLM policy boundary | original acquisition/evidence policy/reference tests; focused context never projects raw Observation payload |
| Derived cache and diagnostics do not become canonical truth | telemetry off/on equality; G1/G2 differential; source-separated benchmark JSON |
| §9.1 independent expected results, no legacy result as automatic truth | primitive oracles plus separate G0-C1 counterexamples/fix; G8 toy ground truth is not generated by score provider |
| §9.2 fixed response/clock tape, real IDs/provenance; no timestamp normalization | G9 compares full canonical objects across concurrency and restart |
| §9.3 per-case metrics, raw samples, alternating pairs, uncertainty, no silent timeout exclusion | bounded runners and JSON receipts; large-scale unresolved measurements remain explicit |
| §10 compatibility and rollback without erasing durable budget/history | runtime/package 0.4.1, checkpoint 4.0, old versions rejected; priority/progress rollback and opaque state tests |
| §11 historical freeze versus explicit current-source approval | historical Git-blob test and approved G9 release hash; post-approval source/reference gate PASS |
| §12 final evidence/status/documentation audit | PASS; current status documents point to this acceptance and preserve historical receipts |

## Conditional work and exclusions

- **G5 DEFERRED — trigger not established.** G0's retained measurements report
  operation-level elapsed time and allocation peaks, not a verified fraction of
  representative workflow time attributable to full-copy/recompute that remains
  >=20% after G1/G2. Inclusive nested spans cannot be summed to manufacture that
  fraction. This is UNKNOWN, not a finding that the fraction is below 20%.
  Existing deep-copy/full-recompute behavior remains. StateDelta and
  ReadDependencies are conditional proposals, not missing unconditional APIs.
- **C1 DEFERRED:** no validator-result memo; depends on a complete G5 read contract.
- **C2 DEFERRED:** no delta/WAL/blob checkpoint redesign or fsync removal. No
  verified conditional checkpoint-share trigger/design approval is asserted.
- **C3 DEFERRED:** no expiry heap, persistent graph index or filesystem cache.
- **C4 DEFERRED:** no learned priority, LLM summary, prompt-cache adapter or
  cross-run memory. Requires separately approved live/provider evaluation.
- QueryIndex/DecisionContextBundle/PerformanceReceipt in the plan were proposed
  contract names. Actual implementations are operation-local Observation lookup,
  search-local graph structures, typed focused context plus diagnostic JSON
  receipts. They do not introduce an authoritative persisted generic cache.

## Final decisions and delivery

1. **Implemented and accepted:** the final-audit index gap is
   closed with 100,000-entry/8-MiB membership-reference caps and full-scan fallback
   shared by knowledge, focused context/retrieval and priority. Overflow never
   publishes a partial index. Differential/overflow/default/compatibility checks
   passed; see G9 receipt. Approved source is
   `7293c3e3ca79ba3177f27ee5a52c48c41719e7573996f1325a522b54b9945ce5`.
2. **Completed:** current-source composition completed 105 timing executions and
   three allocation runs with source/harness integrity and canonical parity PASS.
   G6 final evidence retained a 98.81% synthetic-token reduction with zero required
   omissions; this is not a live-provider claim.
3. **Decision made:** G2 stays opt-in. Although the representative shared-suffix
   case improved, the final fanout-1,000 control's p95 regressed 24.4%. The
   provisional legacy default was reverted without changing thresholds. Both
   legacy and explicit v2 retain their original omitted-policy behavior.
4. Retain G1 commit/freshness default-off; payload's measured small-case peak
   exceeds the original 20% boundary, so no default-enable approval is currently
   supported. Keep serial scheduler and baseline priority defaults.
5. **Completed:** final semantic/source/reference/architecture/determinism/
   compatibility checks and 0.4.1 build passed on the approved source.
6. **Completed:** README/current API guidance and authoritative status/index are
   updated while historical receipts, failed/inconclusive rows and rollback paths
   remain preserved.

**The plan-defined offline objective is complete.** This acceptance does not
extend to live LLM/provider quality, real targets, actual domain bundles or a
universal performance claim; those remain outside the approved scope.
