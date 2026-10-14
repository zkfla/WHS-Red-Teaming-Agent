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

## Completion Criteria

Every requirement above proven against current state, required reviews addressed, meaningful checks pass, stage commit contains scoped files, preserved user state. Move to completed only then; otherwise retain precise missing requirements and next action. Parent goal remains active until all stages including physical proof and final review/repair/test pass.

