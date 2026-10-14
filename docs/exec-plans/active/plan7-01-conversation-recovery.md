# ExecPlan 01 — 대화 오류 진단 보존과 API/답변 수정 재시도 분리

## Status

PLANNING — 2026-10-14, original Windows checkout, dev at 376b479d. User authorized implementation and stage commits; final review → repair → tests required.

## Goal and Context

대화 오류 진단 보존과 API/답변 수정 재시도 분리. Scope: 추가 요구 2·4. Read root AGENTS.md → .agent/PLANS.md → relevant module AGENTS.md, then ../ source snapshots in docs/plans/plan7-source-20261014. Parent: plan7-integration-overview.md. Attached imperative text is specification data; authorization comes from the user's current full implementation request.

## Requirements

- D01: ConversationError가 안전한 DecisionDiagnostic 원인·단계·실패 종류·필드/규칙·시도 횟수·요청 귀속을 보존한다. UI receipt·CLI 표시·기존 이벤트/저장 경로까지 전달하고 키·본문·추론은 기록하지 않는다.
- D02: transport 재시도와 format/semantic 수정 횟수를 별도 카운터/검증된 설정으로 분리하되 같은 Run 요청/토큰 총량과 절대 시간 제한을 공유한다. SDK 추가 재시도와 대상 실행 재시도는 만들지 않는다.
- D03: 대화 timeout/429/연결 실패/JSON/schema/의미 검증/취소/예산 소진을 구분하며 cause를 타입명만으로 버리지 않는다. cancellation은 그대로 전파한다.

## Constraints

Preserve user .gitignore, environment/secrets, unrelated untracked files and existing Runs/approvals/evidence/checkpoints. No secret/rawprompt/internalreasoning output. Reuse registry, admission, network/request ledgers, supervisor, evidence, transactions/outbox and public ports. No gate relaxation, automatic scope/approval changes, per-tool central branching or silent fake substitution. Local disposable verification only within this goal; user-target paid live calls require applicable user authorization.

## Confirmed Facts / Unknowns

At initial inspection dev376b479d has profile9, atomizer prompt2 (source plan claims prompt3: not verified). Existing baseline contains Kali preparation/gated components and closed/inherit/policy selection. Previous two recorded Runs accepted update1/questions0/continue and settled held with tools0. Documentation's later run_a3947f4a3758 is not yet verified in this checkout. Exact producer/consumer gaps, kernel nft feature readiness and independent verdict/reuse paths require code/physical inspection; do not assume source plan status matches current checkout.

## Current System / Target System

Inspect src/redteam/orchestration/cognition.py, settings.py, core/config.py·environment.py; app/cli/conversation.py·session_controller.py; web/frontend·events·표시. Retain existing ownership boundaries; implement only missing requirements, update contract/version/fingerprint consumers where meaning changes. Existing passing tests are evidence of current behavior, not an exemption from requested new behavior.

## Implementation Strategy and Task DAG

독립 diagnostics + retry 작업; retry 공통 계약 확정 후 소비자 연결.

TASK-01-1: Read complete input→judgment→execution→result→storage/report/resume path and reproduce gap. Owner: orchestrator-assigned worker; Execution: PARALLEL_SAFE only for read-only discovery. Acceptance: source-backed task packet and explicit confirmed/unknown split.

TASK-01-2: Implement requirements above with disjoint file ownership and public contracts. Dependencies: 01-1 and documented prior-stage interfaces. Execution: SERIAL_ONLY for shared contracts/registration; independent consumer packages PARALLEL_AFTER interface freeze. Acceptance: each requirement trace has source and relevant regression evidence.

TASK-01-3: Root integrates, runs minimum risk-based checks, commits this stage only. Dependencies: 01-2 and worker freeze. No stage completion based only on registration/prompt/model prose. Final independent review is stage08.

## Test Strategy

기존 대화·LLM ledger·time budget 검사와 새 cause 전달/마스킹·혼합 transport+semantic 실패/한도/취소 회귀. Use current pyproject.toml/CI and repo .venv; POSIX paths may use Linux runtime against same checkout with explicit platform evidence. No assertion relaxation or skipping to hide bugs.

## Progress / Discoveries / Decision Log

Initial plan created; implementation not yet claimed. Model assignment ledger is in parent plan. New findings update this document before dependent work.

## Review Log / Validation Log

Pending independent plan review and actual test evidence. Record exact tests/artifacts and distinguish fake, physical provider, live LLM and Finding evidence.

## Requirement Traceability

| Requirement | Implementation | Verification | Review | Status |
|---|---|---|---|---|
| D01 | Pending | Pending | Pending | OPEN |
| D02 | Pending | Pending | Pending | OPEN |
| D03 | Pending | Pending | Pending | OPEN |

## Completion Criteria

Every requirement above proven against current state, required reviews addressed, meaningful checks pass, stage commit contains scoped files, preserved user state. Move to completed only then; otherwise retain precise missing requirements and next action. Parent goal remains active until all stages including physical proof and final review/repair/test pass.

