# Active candidate contract v2: implementation and live verification

2026-10-14, original Windows checkout on `dev`. User requested v2-only planner candidate output, retaining v1 code without requiring historical compatibility; user also requested live bank verification. DeepSeek 4.1 High orchestrated GPT-6 Luna Extra High workers. Root reviewed/integrated and performed live calls.

## Changes and boundaries

`CandidateHypothesis` inherits the existing base fields and normalization from `Hypothesis`, but its active output contract requires explicit `contract_version: 2`, nonempty registered target references and a nonblank judgment reason. Status is restricted to the five existing candidate states and confidence to 0..1. `HypothesisUpdate.hypothesis` now uses this v2-only type. Therefore both legacy single-stage and default split planner JSON schemas publish version 2 only; missing/v1 versions are rejected at the version field before admission. Independent review receipts are still required for confirmed/rejected updates.

The default split planner prompt explicitly requires v2 and explains that required observations must be actual investigation questions, not merely a candidate update or prose reason. Prompt version is incremented to 2. Historical single-stage profile/prompt bytes are retained. The historical `Hypothesis` implementation remains available for graph reads and older generic producers; it is not accepted as the active candidate proposal type. No old producer was redirected through generic writes to bypass revisioned candidate admission. This is v2-only **planner/update** output, not a claim that every historical graph code path was removed. No v1 migration compatibility work was added.

Updated reporting/web-view fixtures use the active type. Tests cover the schema presented to both planners, same-pass v2 admission and candidate/task revision binding, rejection of omitted/v1 versions, existing review requirements, and the retained historical implementation. Invalid versions now produce `missing`/`literal_error` at `updates[0].hypothesis.contract_version`, replacing the earlier generic after-validator rule.

## Offline verification and review

Root checks: 61 candidate/planner/atomization tests; 30 active-schema/report/web/diagnostic tests; 11 profile/report tests after retaining the frozen legacy profile; full Ruff; full source/tests mypy (779 files). Groups overlap and are not summed. DeepSeek reviewed the schema and verified no generic producer or admission guard was changed. Fake clients in these tests are explicit offline fixtures.

## Live reproduction

Existing bank `127.0.0.1:8081`, existing engagement/MCP scope, native-probes profile, OpenCode Go Muse 1.3 Contributor via `openai/responses/muse-spark-1.3-contributor`, effort High, original budgets/approval gates. Bank container healthy. No automatic approval clicks or scope/Control relaxation.

First exact natural-language input (`대상에 대해서 취약점을 분석해줘`) hit a UI `ConversationError` before any Run was created. That error alone does not disclose whether the conversation decision timed out or failed validation/provider processing; its detailed cause was not captured, so no exact diagnosis is asserted. The first actual Run was then started with `/new` plus the identical objective, explicitly bypassing only the conversational classification step. Two subsequent requests used the original natural-language input in fresh sessions and created Runs normally.

| Run | Input path | Known run tokens | Model requests | Candidate output | Tools | End |
|---|---|---:|---:|---|---:|---|
| `run_ad4374a5210d` | `/new` same objective | 18207 | 1 | v2 candidate accepted | 0 | investigation_held |
| `run_068fa919bf17` | original natural input | 37011 | 2 | v2 candidate accepted | 0 | investigation_held |
| `run_b5c542d8fd35` | original natural input, clarified observation prompt | 19493 | 1 | v2 candidate accepted | 0 | investigation_held |

All three terminal reports have `planning_diagnostic=null`. The former v1/v2 planning-error rejection is gone in these attempts. Stored `hypothesis_planner_pass` records show candidate updates but `questions=[]`, and atomization was skipped with `update_only_no_questions`. No target tool executed, no Finding or mission success was produced. Reports are in `reports/<run>/report.md`, with originals in `data/reports/<run>.json`.

## Remaining issue identified by live verification

The model describes the need for initial observation in its public reason but does not emit investigation questions. A valid update-only proposal is admitted; `HypothesisJudgmentCommitted` is absent from the planner wake-event set, and no ready Task exists. Consequently the central loop ends with `investigation_held` instead of asking for a second plan or executing a probe. The existing update-only path is intentionally preserved in this contract change; changing its wake/termination semantics requires a separate bounded-loop regression and budget review, rather than an automatic retry or weakened validation.

**Result:** v2-only proposal contract and actual v2 candidate admission are verified. End-to-end bank analysis is **not** verified as working: zero tool actions and an update-only/no-question stall remain. The conversation classifier also failed once. These limitations are explicit, not converted into success. Web server remains available with original Muse High settings.
