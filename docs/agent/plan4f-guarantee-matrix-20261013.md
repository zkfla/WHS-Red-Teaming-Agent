# F06 guarantee reconciliation - plan 4f (2026-10-13)

Branch DB. The four implementation plans are retained in
`docs/exec-plans/active/plan4/`. Predecessor plan documents are cited by
name and section; they are not represented as tracked repository paths.
Graph lineage is implemented in commit `8843dce8`.

## 1. F01-F22 guarantee matrix (plan4.md section 2 list)

Status meanings: resolved = missing guarantee re-implemented and covered by
a regression on branch DB; guarantee-verified = guarantee holds in current
code and is covered by an existing regression (fix predates the plan4c-4f
branch commits); unfinished = still missing. Every row names the actual
consumer and the verifying test. The reconciliation establishes the listed
behavioral guarantees; it does not claim identical coverage of every assertion
in each retired test file.

| ID | Original deleted/missing guarantee (plan4.md s2) | Actual current consumer | Test evidence (file::test) | Status |
|----|------------------------------------------------|-------------------------|----------------------------|--------|
| F01 | Final success not blocked when evidence expired or reuse conditions changed during completion review. | central_completion.py complete_goal final SQL commit recheck + evidence_validity.py | test_plan4a_completion.py::test_final_sql_checks_time_and_new_binding_reference, ::test_review_rechecks_only_related_support | guarantee-verified |
| F02 | Observed-expiry passage alone opened no planner re-judgment (refresh_validity was read-only). | app/central_run.py CentralRun.run wakeup + evidence_validity.py ExpiryWatch/queue | test_plan4g_expiry.py::test_expiry_only_triggers_one_bounded_rejudgment, unit test_plan4g_expiry.py::test_time_passage_over_observed_expiry_reports_reason (commit 13f24d90) | resolved |
| F03 | Code-artifact generation, independent review, and lookup deleted; only roles/prompts remained. | code_artifacts.py registered local-tool generate/review/inspect via agent_profile.py stage binding + bootstrap.py assembly | test_plan4e_code_artifacts.py::test_writer_cannot_review_own_artifact, ::test_review_rejection_and_digest_mismatch_are_structured, test_plan4e_assembly.py::test_selected_code_stages_are_registered_stored_and_reported (commit b5ef69ec) | resolved |
| F04 | Reviewed artifacts had no link to isolated synthetic-input validation (CodeFixtureRunner orphaned). | capability/code_fixtures.py CodeFixtureRunner reached through the code_artifacts.py fixture path; one-shot isolated fixtures | test_plan4e_code_artifacts.py::test_fixture_mismatch_timeout_and_held_without_promotion, ::test_fixture_unconfigured_and_changed_inputs_do_not_rerun, ::test_goal_mapping_never_counts_fixture_results (commit b5ef69ec) | resolved |
| F05 | collect_checkpointless_originals had no CLI entry (collect-originals deleted). | app/cli/commands/resume.py collect-originals to orchestration/instance_driver.py InstanceDriver.collect_checkpointless_originals | test_plan4d_collect_reconcile.py::test_collect_originals_cli_collects_only_and_creates_nothing, unit test_plan4d_collect_originals.py::test_checkpointless_missing_original_stays_held (commit ded50633) | resolved |
| F06 | Deleted-test guarantees not reconciled against new checks; pass counts prove nothing. | This matrix document plus per-stage regressions in every other row. | This file; per-row file::test references; test_task_origin_grounding.py final SQL relation proof (fc6c9ba7) and actual synthetic agent smoke (5c659de3). | resolved |
| F07 | Operator guidance/focus stored as applied but absent from planner model input. | specialist_planner.py _advance_central payload + cognition.py + central_runtime.process_controls | test_plan4c_planner.py::test_guidance_focus_and_reprioritize_reach_model_input (commit 1606b45a) | resolved |
| F08 | Stored reprioritize intent rejected on the central path. | OperatorIntentType + central_runtime.py process_controls + operator-intent interpreter | test_plan4c_planner.py::test_reprioritize_orders_real_selection_and_rejects_unknown (commit 1606b45a) | resolved |
| F09 | Released shared resource did not auto-continue the waiting original call. | central_runtime.py ready_workers/drive_ready_tasks/resume_intent | test_plan4b_resume.py::test_resource_release_continues_exact_call_without_recharge, ::test_wait_returns_slot_for_independent_work_and_resumes_within_bounds | guarantee-verified |
| F10 | resume --instance fell through into full run() and started other work. | app/central_run.py CentralRun.resume + resume CLI | test_plan4b_resume.py::test_cli_completed_selection_returns_without_opening_missing_sdk, ::test_selected_unstarted_actor_does_not_read_corrupt_foreign_checkpoint | guarantee-verified |
| F11 | Public complete_task unresolved branch called deleted _commit_review (AttributeError). | semantic_completion.py settle_unresolved sharing the central result contract | test_plan4a_completion.py::test_unresolved_entrypoints_share_central_result | guarantee-verified |
| F12 | Same local resource ID in different service domains merged into one cleanup obligation, allowing false goal_achieved. | resource_cleanup.py resource_node_id domain identity, apply_resource_effects, cleanup_originals | test_plan4a_completion.py::test_resource_domains_survive_native_create_delete_and_goal, ::test_resource_replay_wrong_domain_and_older_removal | guarantee-verified |
| F13 | UNKNOWN retry-unsafe call re-executable under a new task/call ID. | nodes/react.py _bind_call + central_state.py _resource_conflict | test_plan4b_resume.py::test_unknown_exact_physical_call_new_ids_and_independent_context, ::test_unknown_duplicate_protection_survives_original_api_restart | guarantee-verified |
| F14 | Planner called the model after time-budget exhaustion. | reasoning_time.py + engine.py guard_reasoning + metered_llm.py admission hook | test_plan4c_planner.py::test_duration_zero_run_makes_no_model_requests, unit test_plan4c_time_admission.py::test_duration_zero_with_started_at_is_exhausted (commit 1606b45a) | resolved |
| F15 | Excluded unstarted work stayed required=true and blocked completion; no planner cancel/necessity contract. | specialist_tasks.py cancel/required adjustment + process_controls exclude path | test_plan4c_planner.py::test_planner_cancel_unblocks_optional_but_required_still_gates (commit 1606b45a) | resolved |
| F16 | Unrelated optional finished work evidence-reuse condition gated the goal. | central_completion.py goal_inputs related-only support check | test_plan4a_completion.py::test_review_rechecks_only_related_support, ::test_optional_expired_support_does_not_block_independent_goal | guarantee-verified |
| F17 | Registered job.reconcile always refused on central runs. | job_local_tools.py central execution ledger/original-query port reusing central_recovery collect_original/reconcile_originals | test_plan4d_collect_reconcile.py::test_central_job_reconcile_lists_masks_and_collects_without_resubmit (commit ded50633) | resolved |
| F18 | CLI re-parse dropped preserved identity (max_no_progress_turns 1 to 3, binding-changed refusal). | settings.py _retired_identity + central_payload/resume binding | test_plan4b_resume.py::test_cli_preserves_nondefault_passive_identity_and_rejects_actual_override | guarantee-verified |
| F19 | Live actor running but report/status showed finished/unresolved from a stale Mission reason. | app/central_views.py inspect_central_run activation-aware projection | test_plan4b_resume.py::test_active_status_uses_current_actor_and_keeps_previous_reason_readonly | guarantee-verified |
| F20 | Shared graph compared different workers local steps on one global order (late step-1 re-observation dropped). | attack_graph/predicates.py observation-version/lineage + writes.py merge_node version advance + service.py retract with provenance, query-path restore; contract in docs/agent/attack-graph.md | unit test_plan4f_graph_lineage.py::test_late_worker_reobservation_restores_despite_smaller_step, ::test_pre_retraction_snapshot_import_stays_withdrawn, unit test_plan4f_retraction_review.py::test_later_lower_step_retraction_blocks_original_redelivery, integration test_plan4f_integration.py::test_stale_snapshot_then_restore_through_central_store (committed 8843dce8) | resolved |
| F21 | Public retry knobs not applied centrally; fixed by removing the promise and documenting the real failure/re-judgment contract. | settings.py + core/config.py + core/environment.py knob removal/rejection + nodes/execute.py (no silent retry; native failure feedback to the model) | unit test_plan4f21_retry_settings.py::test_retry_knobs_are_not_selectable_settings, ::test_env_retry_setting_is_explicitly_rejected (commits 6635edce, 83cc19d3) | resolved |
| F22 | run_engagement orchestration_config override silently ignored; fixed by explicit reject plus assemble-point boundary. | app/bootstrap.py run_engagement override reject/forward contract | unit test_plan4f22_execution_override.py::test_override_rejected_before_run_entry, ::test_normal_entry_retains_assembled_dependencies (commit 6635edce) | resolved |

Unfinished within plan scope: none on the F01-F22 list. Deliberate,
out-of-scope-by-plan boundaries (not unfinished work): default execution has
no automatic retraction path connected (F20 scope note); no auto-retry queue
was restored (F21 decision); legacy runtime, fixed Path/Step, dual queues,
and planner DAGs stay unrestored per plan4.md section 1.

## 1b. F06 deleted-item reconciliation (git history)

Per plan4f section 1, the section 1 table alone is not sufficient: each
deleted function, registration, public entry, and deleted test is classified
below as retired structure (intentionally unrestored), same guarantee in
current tests, needed gap resolved by plan4c-4f work, or unsupported scope
(never a live-verification target). Deletion evidence from
git log --diff-filter=D and file histories on branch DB.

| Deleted item | History evidence | Classification | Current guarantee |
|---|---|---|---|
| collect-originals CLI module (app/cli/commands/collect_originals.py) | deleted; history 201e1fe1, 4b2774ed | needed gap resolved | restored as explicit command in app/cli/commands/resume.py (ded50633); test_plan4d_collect_reconcile |
| Deleted original-collection tests (test_original_collection_cli, test_checkpointless_original_collection, test_execution_original_collection; CLI test deleted in 4b2774ed) | git show 4b2774ed | listed guarantees checked by current tests | test_plan4d_collect_reconcile.py + unit test_plan4d_collect_originals.py |
| Code-artifact generation/review/lookup registrations in code_artifacts.py (stage-identity check only remained) | history 0dc1401a, 201e1fe1, 4b2774ed | needed gap resolved | rewired in b5ef69ec; test_plan4e_code_artifacts.py, test_plan4e_assembly.py |
| Deleted code/fixture tests (test_code_artifacts, test_code_fixture_validation, test_code_fixture_assembly) | diff-filter=D list | listed guarantees checked by current tests | test_plan4e_code_artifacts.py + unit test_plan4e_code_artifacts.py |
| Deleted expiry tests (test_evidence_expiry_wakeup, 301 lines, and test_evidence_validity_consumers, both deleted in 4b2774ed) | git show 4b2774ed | listed guarantees checked by current tests | test_plan4g_expiry integration + unit (13f24d90) |
| Deleted completion-atomicity test (test_completion_review_atomicity, 691 lines, deleted in 4b2774ed) | git show 4b2774ed | listed guarantees checked by current tests | test_plan4a_completion.py final-commit and related-support tests |
| Central-transition orchestration modules (scheduler, convergence, replanning, reconciliation, planning_graph, planning_evidence, nodes/plan, nodes/aggregate, nodes/worker) and their tests | diff-filter=D list | retired structure | intentionally unrestored per plan4.md section 1; legacy scheduling structures intentionally retired; shared permission, budget, result, resume and cleanup guarantees are assessed individually in the matrix |
| Legacy CLI/specialist/graph-viz modules (app/cli.py, specialist_run.py, attack_graph/visualize.py, fakes/bootstrap.py) and their tests | diff-filter=D list | retired structure | superseded by central run/CLI contracts |
| --runtime legacy flag and legacy resume assembly | plan4.md section 1 | unsupported scope | explicitly not restored; checkpointless CLI covers the supported path |
| Live-provider run tests (test_engine_live_run_flow and live MCP/target suites) | live markers in test names | unsupported scope | live verification out of scope; explicit doubles instead |

## 2. Final multi-feature end-to-end scenario (stages C, D, E, F)

Reference composition of the plan4f section 4 flow through the real current
consumers. Each step names its stage; no graph-only run is presented as
covering all four. Per-step regressions are the tests in section 1; the full
ten-step single run was not re-executed in this docs-only pass, and no live
provider or target step is claimed.

1. (C) Goal intake to two investigation tasks of the same role, independent
   workers progress, mid-run originals and questions shared, then operator
   guidance and priority delivered into the live strategy and decomposition
   model input and real task selection (specialist_planner._advance_central,
   process_controls; cf. test_guidance_focus_and_reprioritize_reach_model_input,
   test_reprioritize_orders_real_selection_and_rejects_unknown).
2. (C) Shared-resource contention: first call finishes, the exact original
   call auto-continues without recharge; approval, job, and UNKNOWN waits
   plus real caps and stop stay distinct
   (test_resource_release_continues_exact_call_without_recharge).
3. (C) Mid-run restart resumes on original action, approval, evidence, and
   usage; --instance handles only the selected instance and never starts
   sibling planners
   (test_cli_completed_selection_returns_without_opening_missing_sdk).
4. (D) Checkpointless post-entry run: explicit collect-originals CLI collects
   originals only, held state kept; no new provider run, actor, or budget
   settlement (test_collect_originals_cli_collects_only_and_creates_nothing).
5. (E) Related originals to code generation, exact-hash independent review
   (writer is not reviewer, digest match), store and lookup, then explicit
   synthetic-input fixture validation; the fixture result is preserved and
   never promoted to Finding or goal success
   (test_writer_cannot_review_own_artifact,
   test_fixture_mismatch_timeout_and_held_without_promotion,
   test_goal_mapping_never_counts_fixture_results).
6. (E/C) Completion review under expiry or condition change refuses success on
   stale related evidence; success only on valid originals, met goals, and
   verified cleanup, with report, status, findings, and approvals agreeing
   with original state
   (test_final_sql_checks_time_and_new_binding_reference,
   test_optional_expired_support_does_not_block_independent_goal).
7. (E) Same local ID in two domains: create both, delete one, success refused
   on the remaining obligation; delete via each original, then success
   allowed; original-to-cleanup links never overwritten
   (test_resource_domains_survive_native_create_delete_and_goal).
8. (C) Unsafe UNKNOWN execution: free slot plus new task ID proposes the same
   call, the new provider entry is refused or held; resolving UNKNOWN from
   the existing original keeps lawful follow-up investigation legal
   (test_unknown_exact_physical_call_new_ids_and_independent_context).
9. (C) Zero time at start means zero new model calls; dropping an unrelated
   optional task (exclude, cancel, non-required) still permits goal
   completion, while losing a truly required condition or review-adopted
   evidence still blocks it (test_duration_zero_run_makes_no_model_requests,
   test_planner_cancel_unblocks_optional_but_required_still_gates).
10. (D/F) CLI resume of a run carrying a non-default past settings identity
    keeps original values and real limits, projects running status while the
    actor waits, and shows the current exit reason only after settlement;
    job.reconcile collects only original results and submits no new job;
    retry and override inputs resolve to explicit reject-or-apply, never
    silent loss
    (test_cli_preserves_nondefault_passive_identity_and_rejects_actual_override,
    test_central_job_reconcile_lists_masks_and_collects_without_resubmit,
    test_retry_knobs_are_not_selectable_settings,
    test_override_rejected_before_run_entry).
    Graph lineage (F) threads through steps 1-10 wherever shared-graph reads
    occur: live re-observation restores on new lineage while pre-retraction
    snapshot imports stay withdrawn
    (test_two_workers_share_lineage_through_central_store,
    test_late_worker_reobservation_restores_despite_smaller_step).

## 3. Verification evidence

- Each matrix row points to the actual consumer and a regression that
  exercises the listed guarantee. Existing stage regressions use explicit
  model, execution, and original-query doubles with temporary SQLite stores.
- The isolated Docker fixture suite passed all four tests: security flags,
  timeout cleanup, host-write/network isolation, and cancellation cleanup.
  This is actual container verification, separate from injected-runner tests.
- Five synthetic MCP services passed the finance-lab smoke check. The rebuilt
  agent image then passed both approval-gated and auto-approved central
  execution checks. External targets and paid model calls were not used.
  `b6ea3be2` supplies the declared checkpoint dependencies; `5c659de3` aligns
  the scripted fixture with the exact host-task evidence and nested completion
  criteria contracts, without weakening runtime admission or assertions.
- Ruff passed the complete CI scope. Mypy passed 735 source files after the
  independent remote UI changes were merged without conflict.
- The first full pytest session was interrupted by a missing current
  directory (2 failed, 358 passed, 21 skipped). Its two affected stage-D
  tests passed in an isolated run with a dedicated temporary directory.
  The controlled full retry passed the previous interruption point, then
  stopped at an optional artifact metadata access in the explicit legacy
  adapter: 1 failed, 1799 passed, 57 skipped. Commit `0e7e333a` fixes that
  compatibility path; nine migration, assembly, and original-collection
  regressions and the affected ruff/mypy checks passed.
- Final pytest on the frozen, merged DB checkout passed: **3671 passed,
  61 skipped in 765.10 seconds**, Python 3.14.8 on WSL Ubuntu. It used a
  dedicated temporary directory and no concurrent pytest sessions. Optional
  live/container/browser opt-in checks account for skips; the four pinned
  Docker fixture cases were executed separately and passed. The optional
  upstream browser suite was skipped because Playwright is not installed.
  The earlier partial runs are retained above as diagnostics, not full passes.
- The complete CI ruff scope and mypy scope passed; both Compose
  configurations validated, the MCP example image built, and synthetic
  finance MCP and central agent/approval checks passed. Verification used
  one Python version locally; the CI Python 3.10 matrix was not run here.
- Opus reviewed stages C, D, E, and F20 with no outstanding blockers after
  fixes. Gemini ran parallel stage-E and supplemental grounding reviews;
  source findings were reconciled against the owned contract. Supplemental
  Opus review of `fc6c9ba7` was attempted but repeatedly rate-limited (429).
  Root source review, nine focused regressions, 45 neighbor regressions,
  final full pytest, and actual rebuilt synthetic agent checks cover that
  subsequent change. The rate-limited review is not claimed as completed.
- The synthetic check exposed a separate pre-review task-origin mismatch:
  measured, same-Run, exact-original direct relations accepted by the result
  reviewer were rejected by the origin precheck. `fc6c9ba7` uses the existing
  owned grounding meaning and carries only the relevant edge hash into final
  SQL settlement. All nine new regressions passed: valid relation, absent or
  forged/foreign/inferred relation, review-time removal, final-commit removal,
  unrelated-edge allowance, and public edge-ID round-trip. Forty-five existing
  completion/analysis neighbors also passed. Broader graph reachability or a
  relation witnessed by another original does not satisfy this contract.

## 4. Supported boundaries

Versioned graph snapshots preserve uncertainty for incomparable past
observations; restoration requires fresh live lineage or a snapshot created
after observing the withdrawal. Legacy entries lacking versions retain their
numeric comparison and are not rewritten. Default central execution does not
add automatic retraction. Retired retry knobs are rejected, and execution
limits remain owned by application assembly.

The ten-step scenario is a composition of the referenced consumer regressions.
It is not claimed as one newly executed monolithic run. No external live
provider, external target, or user database migration is claimed.
