# Bank live repair verification — 2026-10-08

## Outcome

The requested end-to-end chain is verified in `run_e5c20fbdaf8b`: real model investigation of the existing local bank, recon and Web execution, source-backed partial graph recording, successful task and goal settlement, and matching JSON/Markdown reporting. The investigation settled **canonically unresolved**. This is not mission achievement or a confirmed vulnerability.

The run used `chatgpt/gpt-6-luna` through LiteLLM with account1, eleven registered production stdio MCP tools, nine model requests, and eight remote tool executions. Three recon calls and four Web calls succeeded; one TLS inspection failed against the plain HTTP lab and was preserved as an error. All eight executions have native selection, role, work, action, invocation, and evidence linkage. The 41 evidence rows include eight remote originals and system/processing/recording/settlement records; they are not 41 independent remote results.

## Repairs and review decisions

- Principal coverage: preserve the single execution representative while grounding the actual response subjects through per-unit actual-use receipts. Share the account-proof collector across affected consumers. Opus plan/code review identified an unconnected argument and a two-session fallback inconsistency; both were repaired and independently rechecked.
- Stall timing: use the existing public canonical-change contract before ending on a stale no-progress counter. Keep limits, observe-side counting, and existing fingerprint semantics. Independent Opus review approved the final delta. A broader open-hypothesis progress change remains unimplemented; no new settlement phase or tool sequence was added.
- Recording diagnostics: expose safe schema-derived field/rule locations rather than only `bad recording input`, retaining existing validation and masking. The live run preceding the final one demonstrated an actionable location without input values. Opus plan feedback was adopted; final independent Gemini review replaced unavailable Opus after repeated provider 429.
- Recording paths: clarify the public output-relative/envelope-prefixed address contract and support strict string/integer segments in both consumers. Retry without one leading `output` only when the original address is absent. A found value mismatch does not trigger fallback. Independent Gemini plan/code review approved the delta and its regressions.
- Data-only harness: count attempted recording separately from partial persisted writes, and require successful canonical settlement records rather than a generic blocked/stalled report. Existing reports were preserved.

Implementation was delegated principally to DeepSeek 4.1 Flash Max; harness work used Muse 1.3 Extra High. Opus 4.8 High provided primary reviews when available; agy Gemini 3.8 Flash High provided supplementary and fallback independent reviews. Root coordinated scope and disposition of feedback. Live execution was separately delegated; root waited for reports without intervening during execution.

## Final live evidence

Graph recording was called three times with partial-result `ok=false` envelopes. Seven facts were accepted and linked to preserved originals, four open hypotheses have recorded target edges, and three branch notes exist in the evidence store. Nine fact items were refused for target, value, or missing-path reasons. Finding count is zero. Partial storage is verified independently of whole-call success.

`task.settle` returned `ok=true / task_blocked`; `goal.settle` returned `ok=true / investigation_finished`. Named unresolved records `evidence_2c12f8921f74` and `evidence_de2cc04b4df4` exactly match the terminal projection. The task is blocked, the mission is blocked, and no pending/ready/running task remains. This does not mean the success criteria were met: the blocked required task remains incomplete for achievement.

The harness terminal report equals `data/reports/run_e5c20fbdaf8b.json` exactly; Markdown renders that same product report. The separate normalized file is a reduced harness projection. Relevant artifacts:

- `data/react-bank-live-20261008/bank-live-report-20261008T033539Z.json`
- `data/react-bank-live-20261008/bank-live-report-20261008T033539Z.md`
- `data/evidence/run_e5c20fbdaf8b.jsonl`
- `data/react-bank-live-20261008/source-manifest-20261008T033400Z.json`

## Automated verification

The final Linux snapshot is `2026-10-08T03:33:31Z`, manifest SHA-256 `d4aaa49da0d483f8bbe92f699ac350965732f9818d7b6fa393287dc7b91075ad`. Python 3.10.22 and 3.14.8 each completed **3049 passed / 51 existing skips**, Ruff exit zero, and mypy exit zero for 598 files. No new skip/xfail markers were added. Root independently read the final logs and checked all 604 manifest source/test entries against the current worktree: no missing or differing entry after line-ending normalization.

Container CI-equivalent checks passed on the preceding principal-repair snapshot. They were not rerun for subsequent diagnostics/progress/recording-only deltas. Windows-wide checks had platform-dependent failures; final required test/type/lint evidence comes from Linux, not from ignoring those failures. Logs and snapshot evidence are under `data/react-bank-live-20261008/linux-verification-pathfinal/`.

## Limits and preserved state

The unresolved conclusion is grounded by digest-only response evidence, which cannot distinguish private account data from a shared generic response. An independent audit also found that operator `expected_access` declarations existed in engagement metadata but were not exposed in the model public context. Therefore “no supplied authorization oracle” describes the model's view, not the absence of operator configuration. This visibility issue is a follow-up; it is not used to claim a verified finding. The independent body-evidence limitation supports this run's unresolved settlement.

Model token usage was unavailable and is recorded as unknown, not zero measured consumption. The existing local bank, `.env`, user materials, and other contributors' files were preserved. No credentials, full model responses, or private reasoning were added to logs. At the time of this verification, the repair was an uncommitted worktree change on `dev-orchestration`; no commit, push, or integration into `dev` had yet been performed. The user subsequently authorized committing and publishing the verified changes to `dev`.
