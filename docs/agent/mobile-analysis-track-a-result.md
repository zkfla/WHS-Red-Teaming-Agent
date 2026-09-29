# Mobile/Analysis Track A Result

- Work ID: `CAPABILITY-MOBILE-ANALYSIS-TRACK-A-20260930`
- Status: **A1-A6 complete**
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
- [x] A6 run the complete InsecureBankv2 engagement against the live apktool
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

## A6 live engagement

`tests/integration/test_mobile_analysis_agent_live.py` is an opt-in full
engagement. With `REDTEAM_TEST_APKTOOL_E2E_APK` pointing at an authorized APK,
it starts the pinned Docker apktool MCP server and exercises the production
configuration, discovery validation, Control Plane, Analysis Agent, Gateway,
Evidence Store, and Attack Graph.

The InsecureBankv2 run completed four real MCP actions and produced four
successful evidence records. The manifest mapper produced verified findings
for debuggability, explicitly exported components, and dangerous permissions.
The decoded project path was stored on the asset and the final graph integrity
report had no blocking issues.

The live run exposed three deployment gaps that were fixed:

- `asset_ref` now uses the container-visible `/work/InsecureBankv2.apk` path.
- ROE operations now use canonical action keys. The unsupported
  `prohibited_techniques` field was removed; the restrictive capability,
  operation, and MCP tool allowlists continue to exclude install/uninstall,
  UI automation, and runtime instrumentation.
- `orchestration.tool_timeout_seconds` now flows from AppConfig into every
  ToolInvocation. Its default remains 10 seconds; this Docker Desktop
  apktool profile sets 180 seconds within the 600-second engagement budget.

On this Windows Docker Desktop host, `decode_apk` took about 115 seconds and
the complete live test passed in 218.25 seconds.

## Commits

| Commit | Result |
|---|---|
| `7a51328` | Seed mobile-analysis assets and references |
| `1c37a96` | Add the mobile-analysis playbook |
| `5664e7c` | Schedule the bounded Analysis Agent |
| `64e0d11` | Map manifest results to findings and graph relations |
| `2dcf969` | Add autonomous failure/recovery/drift coverage and fix discovered orchestration defects |
| current A6 commit | Add the real apktool engagement test and make its deployment configuration executable |

## Validation

- Focused candidate and A5 suite: `28 passed`
- Expanded orchestration regression selection: `136 passed`
- Real InsecureBankv2 apktool MCP engagement: `1 passed in 218.25s`
- Final config plus Recon/Web regression selection: `121 passed, 3 skipped`
- Changed-file Ruff check: `All checks passed!`

The expanded selection includes candidate compilation, Analysis Agent and
manifest mapping, orchestration nodes, approval resume, reconciliation host,
agent loop, and end-to-end tests. The final regression also covers Recon
providers, Recon-to-Web handoff, Web follow-ups, and Web findings. The three
skips are existing opt-in external MCP discovery checks.
