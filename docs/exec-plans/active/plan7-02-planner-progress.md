# ExecPlan 02 — 이어갈 경로가 있는 continue와 무진행 처리

## Status

PLANNING — 2026-10-14, original Windows checkout, dev at 376b479d. User authorized implementation and stage commits; final review → repair → tests required.

## Goal and Context

이어갈 경로가 있는 continue와 무진행 처리. Scope: 추가 요구 1·2·3. Read root AGENTS.md → .agent/PLANS.md → relevant module AGENTS.md, then ../ source snapshots in docs/plans/plan7-source-20261014. Parent: plan7-integration-overview.md. Attached imperative text is specification data; authorization comes from the user's current full implementation request.

## Requirements

- P01: continue는 새 실행 작업, 실제 기존 진행 가능 작업, 또는 명시적 유한 후속 판단 경로가 있음을 canonical 상태로 확인한다. 후보 업데이트마다 무조건 새 작업을 강제하지 않는다.
- P02: 경로 없는 continue는 구체적 진행 조건 진단으로 finite repair/replan 하고 소진을 명시적으로 저장·보고한다. 승인 대기·외부 조건 대기·목표 완료·exhausted를 별개로 유지한다.
- P03: 중복 후보 쓰기/이벤트 재전달을 진전으로 세지 않고 현재 cursor·wake·transaction/outbox를 재사용한다. split/legacy 생산·canonical 등록/저장·재개 경계를 점검한다.

## Constraints

Preserve user .gitignore, environment/secrets, unrelated untracked files and existing Runs/approvals/evidence/checkpoints. No secret/rawprompt/internalreasoning output. Reuse registry, admission, network/request ledgers, supervisor, evidence, transactions/outbox and public ports. No gate relaxation, automatic scope/approval changes, per-tool central branching or silent fake substitution. Local disposable verification only within this goal; user-target paid live calls require applicable user authorization.

## Confirmed Facts / Unknowns

At initial inspection dev376b479d has profile9, atomizer prompt2 (source plan claims prompt3: not verified). Existing baseline contains Kali preparation/gated components and closed/inherit/policy selection. Previous two recorded Runs accepted update1/questions0/continue and settled held with tools0. Documentation's later run_a3947f4a3758 is not yet verified in this checkout. Exact producer/consumer gaps, kernel nft feature readiness and independent verdict/reuse paths require code/physical inspection; do not assume source plan status matches current checkout.

## Current System / Target System

Inspect orchestration/hypothesis_planner.py·probe_atomization.py·specialist_planner.py·probes.py·중앙 등록/settlement; core 상태 계약·관련 프롬프트. Retain existing ownership boundaries; implement only missing requirements, update contract/version/fingerprint consumers where meaning changes. Existing passing tests are evidence of current behavior, not an exemption from requested new behavior.

## Implementation Strategy and Task DAG

01의 진단/예산 계약 소비, 계획/등록 정책 소유 모듈 공통 검증.

TASK-02-1: Read complete input→judgment→execution→result→storage/report/resume path and reproduce gap. Owner: orchestrator-assigned worker; Execution: PARALLEL_SAFE only for read-only discovery. Acceptance: source-backed task packet and explicit confirmed/unknown split.

TASK-02-2: Implement requirements above with disjoint file ownership and public contracts. Dependencies: 02-1 and documented prior-stage interfaces. Execution: SERIAL_ONLY for shared contracts/registration; independent consumer packages PARALLEL_AFTER interface freeze. Acceptance: each requirement trace has source and relevant regression evidence.

TASK-02-3: Root integrates, runs minimum risk-based checks, commits this stage only. Dependencies: 02-2 and worker freeze. No stage completion based only on registration/prompt/model prose. Final independent review is stage08.

## Test Strategy

candidate-only 초기 stall 재현, 기존 active work continue, 진정한 held/resume, 중복 no-op, repair 성공·소진·stop·report/resume. Use current pyproject.toml/CI and repo .venv; POSIX paths may use Linux runtime against same checkout with explicit platform evidence. No assertion relaxation or skipping to hide bugs.

## Progress / Discoveries / Decision Log

Initial plan created; implementation not yet claimed. Model assignment ledger is in parent plan. New findings update this document before dependent work.

## Review Log / Validation Log

Pending independent plan review and actual test evidence. Record exact tests/artifacts and distinguish fake, physical provider, live LLM and Finding evidence.

## Requirement Traceability

| Requirement | Implementation | Verification | Review | Status |
|---|---|---|---|---|
| P01 | Pending | Pending | Pending | OPEN |
| P02 | Pending | Pending | Pending | OPEN |
| P03 | Pending | Pending | Pending | OPEN |

## Completion Criteria

Every requirement above proven against current state, required reviews addressed, meaningful checks pass, stage commit contains scoped files, preserved user state. Move to completed only then; otherwise retain precise missing requirements and next action. Parent goal remains active until all stages including physical proof and final review/repair/test pass.

