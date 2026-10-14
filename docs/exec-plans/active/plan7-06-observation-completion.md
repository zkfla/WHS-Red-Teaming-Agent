# ExecPlan 06 — 모순 완료 조건과 정당한 원본 재사용

## Status

PLANNING — 2026-10-14, original Windows checkout, dev at 376b479d. User authorized implementation and stage commits; final review → repair → tests required.

## Goal and Context

모순 완료 조건과 정당한 원본 재사용. Scope: plan7d 요구 1–9 + 구체적 완료 진단. Read root AGENTS.md → .agent/PLANS.md → relevant module AGENTS.md, then ../ source snapshots in docs/plans/plan7-source-20261014. Parent: plan7-integration-overview.md. Attached imperative text is specification data; authorization comes from the user's current full implementation request.

## Requirements

- K01: 같은 후보 확인+반증 AND 모순을 구조화 조건으로 생산/등록에서 거절하고 criterion/hypothesis refs를 finite repair에 전달한다. 서로 다른 후보·정상 관측·이미 확정 사실 검증은 유지하며 전역 OR/문구 parsing은 금지한다.
- K02: 관측 원본→관측 반환/검토→Planner 후속 판단→정확한 revision/purpose 독립 candidate verdict 흐름을 연결하고 후보 사전 확정과 Task 완료의 순환 의존을 제거한다. existing condition ID 원본 복원 여부를 먼저 확인한다.
- K03: 원본 task purpose/target/principal/action은 불변이다. 공개 source task/result와 reuse/validity binding/독립 review로 다른 Task의 정당한 재사용을 연결한다. purpose hash 전역 제거·wrong/stale/forged/raw없는receipt 자동 인정은 금지한다.
- K04: 거절을 기존 diagnostics에 원인·소유자·criterion/source refs·repair/resume 조건으로 남긴다. 런타임 결함·대상 전제·정책 대기·증거 미확정을 근거 없이 합치지 않는다.

## Constraints

Preserve user .gitignore, environment/secrets, unrelated untracked files and existing Runs/approvals/evidence/checkpoints. No secret/rawprompt/internalreasoning output. Reuse registry, admission, network/request ledgers, supervisor, evidence, transactions/outbox and public ports. No gate relaxation, automatic scope/approval changes, per-tool central branching or silent fake substitution. Local disposable verification only within this goal; user-target paid live calls require applicable user authorization.

## Confirmed Facts / Unknowns

At initial inspection dev376b479d has profile9, atomizer prompt2 (source plan claims prompt3: not verified). Existing baseline contains Kali preparation/gated components and closed/inherit/policy selection. Previous two recorded Runs accepted update1/questions0/continue and settled held with tools0. Documentation's later run_a3947f4a3758 is not yet verified in this checkout. Exact producer/consumer gaps, kernel nft feature readiness and independent verdict/reuse paths require code/physical inspection; do not assume source plan status matches current checkout.

## Current System / Target System

Inspect core/contracts/completion.py·graph.py; orchestration/specialist_tasks.py·probe_atomization.py·hypothesis_planner.py·completion.py·semantic_completion.py·evidence_validity.py·central_results.py. Retain existing ownership boundaries; implement only missing requirements, update contract/version/fingerprint consumers where meaning changes. Existing passing tests are evidence of current behavior, not an exemption from requested new behavior.

## Implementation Strategy and Task DAG

05 원본/route 전달 이후; 생산/등록 contradiction validator와 review/reuse 소유권 분리.

TASK-06-1: Read complete input→judgment→execution→result→storage/report/resume path and reproduce gap. Owner: orchestrator-assigned worker; Execution: PARALLEL_SAFE only for read-only discovery. Acceptance: source-backed task packet and explicit confirmed/unknown split.

TASK-06-2: Implement requirements above with disjoint file ownership and public contracts. Dependencies: 06-1 and documented prior-stage interfaces. Execution: SERIAL_ONLY for shared contracts/registration; independent consumer packages PARALLEL_AFTER interface freeze. Acceptance: each requirement trace has source and relevant regression evidence.

TASK-06-3: Root integrates, runs minimum risk-based checks, commits this stage only. Dependencies: 06-2 and worker freeze. No stage completion based only on registration/prompt/model prose. Final independent review is stage08.

## Test Strategy

same-candidate contradiction 경계, distinct candidates, observation completion→independent verdict 실제 호출경로, valid followup reuse와 wrongtarget/session/source/stale/rawmissing 거절; 역사적 조회. Use current pyproject.toml/CI and repo .venv; POSIX paths may use Linux runtime against same checkout with explicit platform evidence. No assertion relaxation or skipping to hide bugs.

## Progress / Discoveries / Decision Log

Initial plan created; implementation not yet claimed. Model assignment ledger is in parent plan. New findings update this document before dependent work.

## Review Log / Validation Log

Pending independent plan review and actual test evidence. Record exact tests/artifacts and distinguish fake, physical provider, live LLM and Finding evidence.

## Requirement Traceability

| Requirement | Implementation | Verification | Review | Status |
|---|---|---|---|---|
| K01 | Pending | Pending | Pending | OPEN |
| K02 | Pending | Pending | Pending | OPEN |
| K03 | Pending | Pending | Pending | OPEN |
| K04 | Pending | Pending | Pending | OPEN |

## Original numbered requirement coverage

The grouped rows above do not permit partial PASS. At completion, inspect each original numbered implementation requirement in docs/plans/plan7-source-20261014/plan7d.md and attach producer/consumer/test evidence independently.

| Source requirement | Plan requirement | Evidence / disposition |
|---|---|---|
| plan7d implementation 1 | K01/K02 | OPEN |
| plan7d implementation 2 | K01 | OPEN |
| plan7d implementation 3 | K01 | OPEN |
| plan7d implementation 4 | K02 | OPEN |
| plan7d implementation 5 | K03 | OPEN |
| plan7d implementation 6 | K03 | OPEN |
| plan7d implementation 7 | K04 | OPEN |
| plan7d implementation 8 | K04 | OPEN |
| plan7d implementation 9 | K03/K04 | OPEN |

## Completion Criteria

Every requirement above proven against current state, required reviews addressed, meaningful checks pass, stage commit contains scoped files, preserved user state. Move to completed only then; otherwise retain precise missing requirements and next action. Parent goal remains active until all stages including physical proof and final review/repair/test pass.


Preliminary Sol source review confirms the source7d prior-implementation claim is absent here: AtomizedProbeTask inherits full completion_criteria, validates full hashed condition identity, and atomizer input emits full conditions; criterion-ID selection followed by original-condition restoration is not yet implemented. Same-candidate confirmed/refuted conditions currently pass producer and canonical subject validation, then AND completion blocks before independent Task review. These are concrete gaps, not permission to rewrite historical Tasks. B owns hypothesis_planner.py until its freeze; later06 producer/registration packets require explicit handoff and review.

### Concrete stage06 packets (proposal; no mutation until root review gate)

TASK-06-A — Structured condition consistency (source7d1–3,8). Ownership: core/contracts/completion.py public pure validator; probe_atomization.py producer; specialist_tasks.py producer registration plus central_state.py CentralTransaction.register_task new canonical admission after registration replay lookup and before insertion; bounded dedicated tests. One validator returns conflicting criterion IDs and hypothesis ID only when confirmed/refuted refer to the same candidate; preserve different candidates and established-fact/observation conditions. Invoke before atomizer and direct canonical registration, preserve atomic no-write rejection and existing finite repair budget. Guidance explains ordinary observation/return conditions and AND semantics; natural-language If does not create branches. Prompt ownership coordinated with02-C and06-B, no overlapping writes. Acceptance: conflict rejection at producer/specialist/direct public canonical boundaries, concrete safe refs in repair, normal cases retained, atomic batch rollback, no partial Tasks/candidates committed, replay/read of stored historical contradictory Tasks preserved. Do not add unconditional MissionTask deserialization rejection. Dependencies02-B/C shared-file and prompt freeze; no03–05 completion dependency for this pure producer/admission package; core/module instructions required. No global OR or final-verdict prohibition.

TASK-06-B — Criterion-ID selection and original restoration (source7d9 discrepancy). Ownership: probe_atomization.py after A freeze, its atomizer-facing schema/input/restore, current atomizer prompt/profile and dedicated contract tests; stored Task completion shape remains unchanged. Emit original criterion IDs and bounded original conditions; atomizer selects IDs from its own bound question, code restores exact originals before canonical validation. Reject unknown/duplicate/cross-question IDs and incomplete coverage. Preserve paired conditions, question subjects and original purpose identities. Actual schema/prompt meaning changes require owned version/fingerprint/resume handling, not rewriting historical Runs or Tasks. Dependencies A interface and02-C prompt/profile handoff. Acceptance: model rewording cannot alter a restored condition, original criterion hashes retained, valid selected coverage works, rejected selection commits nothing, historical reads remain.

TASK-06-C — Source/consumer reuse assessment (source7d5–8). Ownership: evidence_validity.py public structural assessment; semantic_completion.py verify_single_origin/_verify_task_origin; completion.py validate_result_claims; central_completion.py actual goal receipt input consumer; dedicated validity/reuse integration tests. Read existing source_task_id/source_result_id, preserved original identity/purpose, target/principal/session, validity snapshots and independent review. Distinguish unchanged origin purpose from different consuming Task purpose, require measured relevant reuse with existing review/binding contract, never automatically treat matching strings or a source pair alone as successful completion. Freeze assessment interface before all consumers change; final canonical commit rechecks consumed proof. Existing records preferred; any new durable meaning must first prove current records insufficient and describe storage/report/resume. Acceptance: valid Task B adoption of Task A original passes independent review and later goal receipt validation; wrong source/result/target/principal/session, stale source purpose/revision, changed validity, forged/missing raw and rawless completion receipt remain rejected. Unbound validity is not blanket denial; no invented expiry, original-purpose integrity removal or evidence database.

TASK-06-D — Callable feedback/review chain and diagnostics (source7d4,7,8). Ownership after producer/reuse freezes: graph_recording.py, task_completion_contract.py/semantic_completion.py only demonstrated missing seam, hypothesis_planner.py safe admission diagnostics and existing event/report consumers only if proven missing; dedicated whole-chain integration tests. Existing graph.record verifier/receipt route remains sole candidate-review producer; v2 Planner owns judgment and canonical exact revision/purpose admission. Start with integration invoking actual graph.record reviewer/provider, not manually fabricating receipt or graph status. Verify preserved original→observation Task return/review→Planner follow-up→independent candidate receipt→atomic judgment; confirmation/refutation/UNKNOWN separately. Carry known purpose/raw/admission rejection codes and bounded criterion/source refs via existing CompletionIssues/DecisionDiagnostic, not plain error-body logs. Task success, candidate verdict and Finding stay separate. Fakes labeled; physical provider proof remains07. No alternate candidate engine.

Sol source review accepted as concrete evidence: candidate contradiction currently survives bind_plan/_validate_subjects; full-object atomizer comparison has no ID restoration; three reuse consumers compare original purpose to consuming Task and goal validation repeats this gate; generic hypothesis_admission_rejected loses known cause. Existing independent candidate-review route is present in graph.recording and must be exercised before patching. Stage06 remains PLANNING until this proposal receives fresh independent plan review and dependencies freeze.


Sol plan review MEDIUM canonical-entry finding accepted: CentralState.register_task directly invokes CentralTransaction.register_task, bypassing specialist validation. A now owns that new-admission boundary after explicit02B central_state.py handoff, replay lookup precedes validation, historical model deserialization remains unchanged. Add direct-public rejection, batch rollback and historical replay/read regressions. Minimal dependency edges:06A waits02B/C file/prompt freeze only;06B waitsA/probe and promptprofile handoff;06C structural assessment may freeze independently with bound-original fixtures, execution-specific proof consumes03–05;06D full-chain proof follows producer/reuse freezes and03–05. Fresh review requested for this correction; mutation still gated.

Fresh Sol plan recheck found no remaining concrete blocker after the canonical-entry and dependency correction. This is plan consistency only. Root authorizes06-C structural assessment contract proposal/fixture reproduction independently; consumer mutation requires public interface freeze.06-A/B wait02B/C shared-owner handoff,06-D waitsproducer/reuse andexecutioninterfaces.
