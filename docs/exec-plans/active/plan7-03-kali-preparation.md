# ExecPlan 03 — Kali 설정·준비·조립 연결

## Status

PLANNING — 2026-10-14, original Windows checkout, dev at 376b479d. User authorized implementation and stage commits; final review → repair → tests required.

## Goal and Context

Kali 설정·준비·조립 연결. Scope: plan7a 요구 1–7. Read root AGENTS.md → .agent/PLANS.md → relevant module AGENTS.md, then ../ source snapshots in docs/plans/plan7-source-20261014. Parent: plan7-integration-overview.md. Attached imperative text is specification data; authorization comes from the user's current full implementation request.

## Requirements

- A01: closed/inherit/명시 정책/허용 대상 정책 준비의 웹→snapshot→CLI 조립 흐름을 연결하고 명시 closed가 환경 inherit로 뒤집히지 않는다. 기존 폼/검증 계약을 확장하며 수동 JSON 필수 접점을 보완한다.
- A02: bounded budget·registry pin·immutable sidecar·현재 Docker endpoint·Kali image/program manifest를 검증한다. 선택한 daemon에서 실제 제한된 nft readiness를 확인하고 준비 cache/fingerprint를 연결한다.
- A03: local-only와 target-ready·이미지 누락·daemon 불가·kernel 미지원·정책 불일치를 구분해 입력/조치에 전달한다. 조회만으로 build/model 호출하지 않고 정책/이미지 변화의 resume identity를 보존한다.

## Constraints

Preserve user .gitignore, environment/secrets, unrelated untracked files and existing Runs/approvals/evidence/checkpoints. No secret/rawprompt/internalreasoning output. Reuse registry, admission, network/request ledgers, supervisor, evidence, transactions/outbox and public ports. No gate relaxation, automatic scope/approval changes, per-tool central branching or silent fake substitution. Local disposable verification only within this goal; user-target paid live calls require applicable user authorization.

## Confirmed Facts / Unknowns

At initial inspection dev376b479d has profile9, atomizer prompt2 (source plan claims prompt3: not verified). Existing baseline contains Kali preparation/gated components and closed/inherit/policy selection. Previous two recorded Runs accepted update1/questions0/continue and settled held with tools0. Documentation's later run_a3947f4a3758 is not yet verified in this checkout. Exact producer/consumer gaps, kernel nft feature readiness and independent verdict/reuse paths require code/physical inspection; do not assume source plan status matches current checkout.

## Current System / Target System

Inspect app/web/engagement*·기존 fields/provider·cli/commands/run.py; capability/shell_runtime.py·shell_preparation.py·shell_assembly.py·gated_activation.py·gated_network_policy.py. Retain existing ownership boundaries; implement only missing requirements, update contract/version/fingerprint consumers where meaning changes. Existing passing tests are evidence of current behavior, not an exemption from requested new behavior.

## Implementation Strategy and Task DAG

01–02와 의미적으로 독립 조사 가능; 소스 구현은 정책/조립 소유권 확정 후, 7b보다 선행.

TASK-03-1: Read complete input→judgment→execution→result→storage/report/resume path and reproduce gap. Owner: orchestrator-assigned worker; Execution: PARALLEL_SAFE only for read-only discovery. Acceptance: source-backed task packet and explicit confirmed/unknown split.

TASK-03-2: Implement requirements above with disjoint file ownership and public contracts. Dependencies: 03-1 and documented prior-stage interfaces. Execution: SERIAL_ONLY for shared contracts/registration; independent consumer packages PARALLEL_AFTER interface freeze. Acceptance: each requirement trace has source and relevant regression evidence.

TASK-03-3: Root integrates, runs minimum risk-based checks, commits this stage only. Dependencies: 03-2 and worker freeze. No stage completion based only on registration/prompt/model prose. Final independent review is stage08.

## Test Strategy

closed/inherit/policy/preparation 저장·전달 fixture, readiness 오류 분류, image/sidecar/context identity와 resume; 실제 nft --check는 별도 증거. Use current pyproject.toml/CI and repo .venv; POSIX paths may use Linux runtime against same checkout with explicit platform evidence. No assertion relaxation or skipping to hide bugs.

## Progress / Discoveries / Decision Log

Initial plan created; implementation not yet claimed. Model assignment ledger is in parent plan. New findings update this document before dependent work.

Root preparation evidence: current Docker desktop-linux kernel6.18.33.2-microsoft-standard-WSL2, sidecar sha256:8092cce5aa8856b9b44016973a835c8e255b46f2cf3f071b5c7ff635cdba23e8. Production build_ruleset(queue7, allowed empty, quota4096) passed actual nft --check in disposable network-none/cap-dropALL+NET_ADMIN sidecar. Binary LF stdin was used after PowerShell text piping produced CRLF syntax errors. This is readiness evidence only, not target packet execution/ledger proof. Source plan run_a3947f4a3758 has zero state_requests in this checkout; its reported execution is unverified here.

## Review Log / Validation Log

Pending independent plan review and actual test evidence. Record exact tests/artifacts and distinguish fake, physical provider, live LLM and Finding evidence.

## Requirement Traceability

| Requirement | Implementation | Verification | Review | Status |
|---|---|---|---|---|
| A01 | Pending | Pending | Pending | OPEN |
| A02 | Pending | Pending | Pending | OPEN |
| A03 | Pending | Pending | Pending | OPEN |

## Completion Criteria

Every requirement above proven against current state, required reviews addressed, meaningful checks pass, stage commit contains scoped files, preserved user state. Move to completed only then; otherwise retain precise missing requirements and next action. Parent goal remains active until all stages including physical proof and final review/repair/test pass.

