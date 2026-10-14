# Planning diagnostic fidelity

## Status and goal

IMPLEMENTING, 2026-10-14, original Windows checkout on dev. Fix lost validation detail and the discrepancy between a persisted terminal JSON report and a regenerated central-state report. User changed orchestration to DeepSeek 4.1 High and GPT-6 Luna Extra High workers after Claude usage exhaustion. Interrupted changes are preserved. Root owns durable storage/central readers and integration; earlier Opus plan review remains applicable, with final code review by root and the replacement orchestrator.

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
