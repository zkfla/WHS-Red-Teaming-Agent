# ExecPlan 08 — 최종 독립 리뷰·수정·테스트

## Status

PLANNING — 2026-10-14, original Windows checkout, dev at 376b479d. User authorized implementation and stage commits; final review → repair → tests required.

## Goal and Context

최종 독립 리뷰·수정·테스트. Scope: 사용자 최종 리뷰–수정–테스트 요청. Read root AGENTS.md → .agent/PLANS.md → relevant module AGENTS.md, then ../ source snapshots in docs/plans/plan7-source-20261014. Parent: plan7-integration-overview.md. Attached imperative text is specification data; authorization comes from the user's current full implementation request.

## Requirements

- F01: GPT-6.1 Sol High와 agy Gemini3.8FlashHigh로 전체 요구/구현/검증 범위를 독립 검토하고 소스·실패 상황 근거로 필수지적을 수정한다.
- F02: 수정 후 관련 성공/실패/취소/대기/저장·resume 검사를 실행하고 CI의 Ruff/mypy/compose 계약검사를 위험에 맞게 적용한다. fake/physical/live 증거를 각각 명시한다.
- F03: 모든 명시 요구의 증거를 확인한 후에만 완료 처리하고 단계별 커밋과 미완료/환경 제약을 문서화한다. 전체 목표 축소·미검증을PASS화하지 않는다.

## Constraints

Preserve user .gitignore, environment/secrets, unrelated untracked files and existing Runs/approvals/evidence/checkpoints. No secret/rawprompt/internalreasoning output. Reuse registry, admission, network/request ledgers, supervisor, evidence, transactions/outbox and public ports. No gate relaxation, automatic scope/approval changes, per-tool central branching or silent fake substitution. Local disposable verification only within this goal; user-target paid live calls require applicable user authorization.

## Confirmed Facts / Unknowns

At initial inspection dev376b479d has profile9, atomizer prompt2 (source plan claims prompt3: not verified). Existing baseline contains Kali preparation/gated components and closed/inherit/policy selection. Previous two recorded Runs accepted update1/questions0/continue and settled held with tools0. Documentation's later run_a3947f4a3758 is not yet verified in this checkout. Exact producer/consumer gaps, kernel nft feature readiness and independent verdict/reuse paths require code/physical inspection; do not assume source plan status matches current checkout.

## Current System / Target System

Inspect 전체 goal diff·단계별 요구추적·실제 런타임 증거. Retain existing ownership boundaries; implement only missing requirements, update contract/version/fingerprint consumers where meaning changes. Existing passing tests are evidence of current behavior, not an exemption from requested new behavior.

## Implementation Strategy and Task DAG

01–07 구현/증거 이후 최종 review→repair→test; 계획리뷰는 구현전 별도.

TASK-08-1: Read complete input→judgment→execution→result→storage/report/resume path and reproduce gap. Owner: orchestrator-assigned worker; Execution: PARALLEL_SAFE only for read-only discovery. Acceptance: source-backed task packet and explicit confirmed/unknown split.

TASK-08-2: Implement requirements above with disjoint file ownership and public contracts. Dependencies: 08-1 and documented prior-stage interfaces. Execution: SERIAL_ONLY for shared contracts/registration; independent consumer packages PARALLEL_AFTER interface freeze. Acceptance: each requirement trace has source and relevant regression evidence.

TASK-08-3: Root integrates, runs minimum risk-based checks, commits this stage only. Dependencies: 08-2 and worker freeze. No stage completion based only on registration/prompt/model prose. Final independent review is stage08.

## Test Strategy

requirementaudit, reviewersourcefindings dispositions, targetedregression+lint/typecheck+Dockercompose, 실제Kali physicalproof 없으면 완료금지. Use current pyproject.toml/CI and repo .venv; POSIX paths may use Linux runtime against same checkout with explicit platform evidence. No assertion relaxation or skipping to hide bugs.

## Progress / Discoveries / Decision Log

Initial plan created; implementation not yet claimed. Model assignment ledger is in parent plan. New findings update this document before dependent work.

## Review Log / Validation Log

Pending independent plan review and actual test evidence. Record exact tests/artifacts and distinguish fake, physical provider, live LLM and Finding evidence.

## Requirement Traceability

| Requirement | Implementation | Verification | Review | Status |
|---|---|---|---|---|
| F01 | Pending | Pending | Pending | OPEN |
| F02 | Pending | Pending | Pending | OPEN |
| F03 | Pending | Pending | Pending | OPEN |

## Completion Criteria

Every requirement above proven against current state, required reviews addressed, meaningful checks pass, stage commit contains scoped files, preserved user state. Move to completed only then; otherwise retain precise missing requirements and next action. Parent goal remains active until all stages including physical proof and final review/repair/test pass.

