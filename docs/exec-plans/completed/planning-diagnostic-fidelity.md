# Planning diagnostic fidelity

## Status and goal

COMPLETED (diagnostic fidelity and requested live diagnosis), 2026-10-14, original Windows checkout on dev. Fix lost validation detail and the discrepancy between a persisted terminal JSON report and a regenerated central-state report. User changed orchestration to DeepSeek 4.1 High and GPT-6 Luna Extra High workers after Claude usage exhaustion. Root owns durable storage/central readers and integration; earlier Opus plan review and final DeepSeek/root code reviews are resolved.

## Facts, requirements and boundaries

Run run_d3ccaec68131 stopped before atomization or tool execution: four planner proposal rejections, normal_used=1, repair_used=3, code decision_repair_budget_exhausted, last_cause hypothesis_proposal_invalid. Its original data/reports JSON contains the diagnostic; report generate reconstructed RuntimeState without it. Exact rejected fields were not recorded and cannot be recovered retrospectively.

- FR-1: preserve schema-owned field locations, validation rule types and exception class/chain through all planner/atomizer validation callbacks, without raw inputs, exception messages or model responses.
- FR-2: retain coded semantic proposal failures and existing retry/admission behavior.
- FR-3: persist terminal diagnostic/history in existing central state_requests, bound to mission identity/revision/status/reason; expose matching diagnostics through read-only report reconstruction.
- FR-4: stale snapshots cannot appear as a current diagnostic after revision changes or active work. Empty later diagnostics clear earlier failures.
- FR-5: recover this historical report into a separate diagnostic-recovered Markdown using the preserved JSON, with Run/mission/reason matched to central originals. No implicit import/backfill during reads.
- Constraints: preserve user .gitignore/untracked files and investigation files; no live LLM/target calls, validation relaxation, increased budgets or private reasoning capture.

## Tasks and dependency order

| Task | Owner | Files/boundary | Dependencies | Acceptance |
|---|---|---|---|---|
| T1 validation fidelity | Luna worker via DeepSeek | hypothesis_planner.py + targeted tests | Existing diagnostics public helper | Missing/nested/extra field detail and coded semantic errors retained safely |
| T2 durable/report fidelity | Root | central_state.py, central_run.py, central_views.py + integration tests | Existing state_requests + DecisionDiagnostic | Close/reopen rendering, stale revision and clearing tests |
| T3 historical recovery | Root | Separate report-diagnostic-recovered.md | Existing preserved JSON | Matching Run/mission/reason, original MD unchanged |
| T4 integration/review | Root + reviewer | Relevant tests, lint/type checks, docs | T1/T2 | No unresolved concrete blocker |

## Progress and review decisions

T2 tests: 3 PASS on Windows, including cancellation without a mission terminal reason; scoped Ruff passes. Save uses an exact mission snapshot; partial settlement with active actors or absent terminal reason skips publication instead of turning a valid pause/stop into a persistence failure. Reader refuses stale/mismatched snapshots and never mutates storage. No database schema migration/new store is needed.

T3 completed: reports/run_d3ccaec68131/report-diagnostic-recovered.md restores the original terminal diagnostic and trace; no new execution/API call. The original MD remains unchanged.

Windows neighboring completion/recovery tests: 32 PASS and one 2-second startup timeout before the modified settlement path; the same timeout reproduced independently. Linux completion/recovery/diagnostic tests passed 35, and additional report/resume/approval/CLI tests passed 41 without relaxing assertions. Full source mypy passed 421 files using a separate Linux cache; a first attempt using a shared Windows cache hit a mypy internal error.

Plan review: keep exact mission revision/status/reason binding; never implicitly trust/import an old report on reads; use diagnostics' existing safe schema/error handling. Historical recovery is a separate rendering operation rather than a product migration.

## Completion criteria

FR-1 through FR-5 verified, relevant regression and lint/type checks pass, independent review resolved, final scoped diff documented. Runtime use requires a server restart after code changes; no automatic investigation retry is authorized.

## Safe pause and resume handoff

User requested a safe pause before implementation completion. DeepSeek orchestrator and Luna worker were interrupted; earlier Claude agents are already stopped/completed. Durable report changes and three regression tests are committed locally as `4e55342b`; no push or web server restart has been performed for this task. Existing app and bank servers remain available.

Uncommitted `hypothesis_planner.py` contains the interrupted validation-fidelity implementation. Do not treat it as complete: the new planned integration test file does not exist yet, and this partial patch still needs review, meaningful loop regression tests and final validation. Preserve user `.gitignore`, engagement files and unrelated untracked files.

On resume, use DeepSeek 4.1 High orchestrator and GPT-6 Luna Extra High worker. Inspect the current diff first; verify each callback uses its actual public schema and preserves the existing uncoded `hypothesis_proposal_invalid` fallback plus coded semantic failures. Complete missing/nested/extra validation, atomizer, legacy and redaction regression tests. Review the committed storage/read paths as well, finish scoped commits and push, then restart the idle web server only after confirming saved key presence without displaying secrets. Live testing is now explicitly authorized: repeat the original bank analysis request up to three times initially, preserving the original scope, settings and approval gates.

Evidence available at pause: Windows durable/report tests 3 PASS; Linux completion/recovery/report tests 35 PASS before the added cancellation test; additional report/resume/approval/CLI tests 41 PASS; full Ruff PASS; full source/tests mypy 775 files PASS with isolated Linux cache. These checks do not validate the unfinished worker patch. Historical recovered report: `reports/run_d3ccaec68131/report-diagnostic-recovered.md`.

## Resumed completion and live findings

T1 completed by Luna: exact owner schemas supplied, original generic/coded error behavior retained, safe structured details added to all three callbacks. Six new tests cover legacy, split planner, atomizer, semantic codes, redaction and the unmodified outer schema gate. Worker related suites passed 113; DeepSeek independently repeated the new planner/central tests (10 PASS). Root Linux planner/v3/central suites passed 32. The reviewed STOP-after-existing-terminal-reason regression now skips mismatched publication rather than raising; central tests pass 4.

Live testing was separately authorized on resume. Three fresh sessions using the exact original request against the local bank all ended in planning_error before tool actions. The third run identifies candidate_update_requires_v2 at updates[0]: the proposal schema permits/defaults historical v1 while candidate updates require v2. Existing semantic validator branches now have safe static rule IDs; their acceptance conditions are unchanged. An empty-judgment termination/resume-condition error was also observed. Functional bank analysis still fails; proposal-schema alignment is a confirmed follow-up, not included in this diagnostic-fidelity completion.

Full Ruff passes; full source/tests mypy passes 777 files. Candidate rule/new planner tests pass 8; DeepSeek reviewed the static rule IDs and confirmed their parent-schema diagnostic locations. Detailed results and evidence are in `docs/agent/planning-live-diagnosis-20261014.md`. Web server restarted with the diagnostic fixes; original Muse High settings restored through normal API. No target tool ran in the three attempts.

Implementation commits: 4e55342b (durable reports), 797c8a80 (validation fidelity and stop compatibility), 6579fd8a (candidate rejection rule IDs). Final documentation and push follow after final diff/evidence checks.
