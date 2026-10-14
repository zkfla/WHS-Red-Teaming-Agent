# ExecPlan 05 — 공통 워커 Kali 선택·실행·원본 피드백

## Status

PLANNING — 2026-10-14, original Windows checkout, dev at 376b479d. User authorized implementation and stage commits; final review → repair → tests required.

## Goal and Context

공통 워커 Kali 선택·실행·원본 피드백. Scope: plan7c 요구 1–10. Read root AGENTS.md → .agent/PLANS.md → relevant module AGENTS.md, then ../ source snapshots in docs/plans/plan7-source-20261014. Parent: plan7-integration-overview.md. Attached imperative text is specification data; authorization comes from the user's current full implementation request.

## Requirements

- C01: 현재 gated descriptors/readiness·대상/port/program manifest를 공통 role selector/catalog/discovery로 워커·플래너에 전달한다. paging/filter/describe와 context bound로 수백 카드 부담을 줄이고 정적 프로그램 allowlist/필수 Kali 행동열을 만들지 않는다.
- C02: proposal→Control→gateway→protected supervisor/job→원본 수집의 기존 경로를 통과하고 approval 후 같은 invocation을 한 번 이어간다. exit/stdout/stderr/truncation/timeout/script digest/owner/route/gate refs를 원본에 저장한다.
- C03: bounded 원본 출력과 source/action/invocation·target·UNKNOWN을 다음 워커와 Planner가 소비한다. shell gate 증명과 프로그램 주장·관측·Finding을 구분하고 arbitrary stdout을 신뢰된 HTTP/session 증명으로 승격하지 않는다.

## Constraints

Preserve user .gitignore, environment/secrets, unrelated untracked files and existing Runs/approvals/evidence/checkpoints. No secret/rawprompt/internalreasoning output. Reuse registry, admission, network/request ledgers, supervisor, evidence, transactions/outbox and public ports. No gate relaxation, automatic scope/approval changes, per-tool central branching or silent fake substitution. Local disposable verification only within this goal; user-target paid live calls require applicable user authorization.

## Confirmed Facts / Unknowns

At initial inspection dev376b479d has profile9, atomizer prompt2 (source plan claims prompt3: not verified). Existing baseline contains Kali preparation/gated components and closed/inherit/policy selection. Previous two recorded Runs accepted update1/questions0/continue and settled held with tools0. Documentation's later run_a3947f4a3758 is not yet verified in this checkout. Exact producer/consumer gaps, kernel nft feature readiness and independent verdict/reuse paths require code/physical inspection; do not assume source plan status matches current checkout.

## Current System / Target System

Inspect orchestration/tool_discovery.py·agents/tool_access.py·nodes/react.py·native_local_tools.py·probes.py·hypothesis_planner.py·observations/shell_execution.py; capability/shell_adapter.py·manifest/discovery. Retain existing ownership boundaries; implement only missing requirements, update contract/version/fingerprint consumers where meaning changes. Existing passing tests are evidence of current behavior, not an exemption from requested new behavior.

## Implementation Strategy and Task DAG

04 검증된 route/admission 후; 02 진행 계약과 01 진단 소비.

TASK-05-1: Read complete input→judgment→execution→result→storage/report/resume path and reproduce gap. Owner: orchestrator-assigned worker; Execution: PARALLEL_SAFE only for read-only discovery. Acceptance: source-backed task packet and explicit confirmed/unknown split.

TASK-05-2: Implement requirements above with disjoint file ownership and public contracts. Dependencies: 05-1 and documented prior-stage interfaces. Execution: SERIAL_ONLY for shared contracts/registration; independent consumer packages PARALLEL_AFTER interface freeze. Acceptance: each requirement trace has source and relevant regression evidence.

TASK-05-3: Root integrates, runs minimum risk-based checks, commits this stage only. Dependencies: 05-2 and worker freeze. No stage completion based only on registration/prompt/model prose. Final independent review is stage08.

## Test Strategy

schema-sensitive fake model→실제 구성소비 fixture, 미준비/unsupported/approval-resume/failure-UNKNOWN-truncation, 원본→worker/Planner bounded 전달. Use current pyproject.toml/CI and repo .venv; POSIX paths may use Linux runtime against same checkout with explicit platform evidence. No assertion relaxation or skipping to hide bugs.

## Progress / Discoveries / Decision Log

Initial plan created; implementation not yet claimed. Model assignment ledger is in parent plan. New findings update this document before dependent work.

## Review Log / Validation Log

Pending independent plan review and actual test evidence. Record exact tests/artifacts and distinguish fake, physical provider, live LLM and Finding evidence.

## Requirement Traceability

| Requirement | Implementation | Verification | Review | Status |
|---|---|---|---|---|
| C01 | Pending | Pending | Pending | OPEN |
| C02 | Pending | Pending | Pending | OPEN |
| C03 | Pending | Pending | Pending | OPEN |

## Completion Criteria

Every requirement above proven against current state, required reviews addressed, meaningful checks pass, stage commit contains scoped files, preserved user state. Move to completed only then; otherwise retain precise missing requirements and next action. Parent goal remains active until all stages including physical proof and final review/repair/test pass.

