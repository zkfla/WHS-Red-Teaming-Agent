# Mobile/Analysis Track A Result

- Work ID: `CAPABILITY-MOBILE-ANALYSIS-TRACK-A-20260930`
- Status: **A1-A5 complete; A6 pending**
- Branch: `feat/capability-mobile-analysis`
- Base: `origin/capability` at `1ef55563483bc30fd3c0ef2faae852dca3ec000a`
- Integration rule: `dev` was not merged, fast-forwarded, or modified

## Completion checklist

- [x] A1 seed authorized APK assets and stable asset/APK references.
- [x] A2 add the bounded static-analysis playbook.
- [x] A3 register a schedulable Analysis Agent with capability boundaries.
- [x] A4 map manifest observations into evidence-backed graph findings.
- [x] A5 exercise autonomous execution, failure, approval resume, timeout,
  and MCP schema-drift paths.
- [ ] A6 run the complete InsecureBankv2 engagement against the live apktool
  MCP and run the final Recon/Web regression selection.

## Implemented flow

An allowed APK is seeded as an `ASSET` node with `asset_ref`, `apk_ref`, and
`target_ref`. The Analysis Agent binds that node into four bounded read-only
steps:

1. `analysis.decode_apk`
2. `analysis.get_manifest`
3. `analysis.list_smali_files`
4. `analysis.search_in_files`

The decoded `project_dir` is written back to the asset and feeds the remaining
steps. Manifest mapping parses only a successful bounded XML response and
checks debuggability, explicit cleartext configuration, explicitly exported
components, and dangerous permissions. Confirmed violations create verified
findings linked to their target, hypothesis, and evidence.

## A5 corrections and coverage

The end-to-end suite found and fixed two orchestration defects:

- Candidate identity now includes `PlanStep.attempt_record_key`. Distinct
  analysis steps that intentionally share one canonical operation no longer
  collide in the Work Ledger.
- A fresh `AgentLoopRunner` entering through `reconcile_once` binds its
  checkpoint thread to the supplied run ID. It can therefore resume an
  approved action from a SQLite checkpoint without starting a new run.

`tests/integration/test_mobile_analysis_agent_e2e.py` uses the real Control
Plane, MCP registry/gateway, Analysis Agent provider, Work Ledger, Evidence
Store, and Attack Graph with a deterministic apktool protocol adapter. It
covers:

- autonomous four-step execution and verified manifest findings;
- decode failure with failed work and evidence but no finding;
- approval pending followed by same-run resume in a new runner;
- gateway timeout with failed evidence but no finding; and
- a newly required MCP input field rejected before adapter invocation.

## Commits

| Commit | Result |
|---|---|
| `7a51328` | Seed mobile-analysis assets and references |
| `1c37a96` | Add the mobile-analysis playbook |
| `5664e7c` | Schedule the bounded Analysis Agent |
| `64e0d11` | Map manifest results to findings and graph relations |
| current A5 commit | Add autonomous failure/recovery/drift coverage and fix discovered orchestration defects |

## Validation

- Focused candidate and A5 suite: `28 passed`
- Expanded orchestration regression selection: `136 passed`
- Changed-file Ruff check: `All checks passed!`

The expanded selection includes candidate compilation, Analysis Agent and
manifest mapping, orchestration nodes, approval resume, reconciliation host,
agent loop, and end-to-end tests. A6 remains the next authorized step.
