# ExecPlan 04 — Docker 대상 route와 네트워크 gate 정합성

## Status

PLANNING — 2026-10-14, original Windows checkout, dev at 376b479d. User authorized implementation and stage commits; final review → repair → tests required.

## Goal and Context

Docker 대상 route와 네트워크 gate 정합성. Scope: plan7b 요구 1–8. Read root AGENTS.md → .agent/PLANS.md → relevant module AGENTS.md, then ../ source snapshots in docs/plans/plan7-source-20261014. Parent: plan7-integration-overview.md. Attached imperative text is specification data; authorization comes from the user's current full implementation request.

## Requirements

- B01: host loopback·동일 daemon container·일반 remote·remote daemon 뒤 대상의 도달 경로를 구분하고 논리 대상/physical endpoint binding을 operator가 명시한다. 자동 주소 치환/터널/허가 확대 없이 원래 대상과 endpoint를 검증한다.
- B02: 기존 target shell/window와 pins·scope·approval hash·deny 우선·flow/byte/time ledger·namespace 경계를 유지한다. model에 grant/epoch/holder 생성이나 NET_ADMIN/host network/privileged 권한을 주지 않는다.
- B03: 같은 instance 충돌·timeout·stop·취소·sidecar 실패·job terminal에서 gate 닫기/settlement/원래 invocation을 보존한다. 보호된 결과 부재는 UNKNOWN이며 재실행 ID·쿼터 초기화로 복구하지 않는다.
- B04: TCP/UDP gate는 HTTP method/path/header/session 제한을 강제하지 않는다. 적용되는 공통 ROE 제한을 강제할 수 없는 route는 정책·kernel 준비가 성공해도 unavailable이다. Model 프로세스에 CAP_NET_RAW도 부여하지 않는다.

## Constraints

Preserve user .gitignore, environment/secrets, unrelated untracked files and existing Runs/approvals/evidence/checkpoints. No secret/rawprompt/internalreasoning output. Reuse registry, admission, network/request ledgers, supervisor, evidence, transactions/outbox and public ports. No gate relaxation, automatic scope/approval changes, per-tool central branching or silent fake substitution. Local disposable verification only within this goal; user-target paid live calls require applicable user authorization.

## Confirmed Facts / Unknowns

At initial inspection dev376b479d has profile9, atomizer prompt2 (source plan claims prompt3: not verified). Existing baseline contains Kali preparation/gated components and closed/inherit/policy selection. Previous two recorded Runs accepted update1/questions0/continue and settled held with tools0. Documentation's later run_a3947f4a3758 is not yet verified in this checkout. Exact producer/consumer gaps, kernel nft feature readiness and independent verdict/reuse paths require code/physical inspection; do not assume source plan status matches current checkout.

## Current System / Target System

Inspect capability/gated_strategy.py·gated_window.py·gate_observer.py·shell_workroom.py·gated_manifest.py; core/contracts/target.py·network.py; control/network_ledger.py. Retain existing ownership boundaries; implement only missing requirements, update contract/version/fingerprint consumers where meaning changes. Existing passing tests are evidence of current behavior, not an exemption from requested new behavior.

## Implementation Strategy and Task DAG

03 준비/설정 완료 후 구현; 필요한 최소 공통 binding 변경은 생산/Control/증거/fingerprint 함께.

TASK-04-1: Read complete input→judgment→execution→result→storage/report/resume path and reproduce gap. Owner: orchestrator-assigned worker; Execution: PARALLEL_SAFE only for read-only discovery. Acceptance: source-backed task packet and explicit confirmed/unknown split.

TASK-04-2: Implement requirements above with disjoint file ownership and public contracts. Dependencies: 04-1 and documented prior-stage interfaces. Execution: SERIAL_ONLY for shared contracts/registration; independent consumer packages PARALLEL_AFTER interface freeze. Acceptance: each requirement trace has source and relevant regression evidence.

TASK-04-3: Root integrates, runs minimum risk-based checks, commits this stage only. Dependencies: 04-2 and worker freeze. No stage completion based only on registration/prompt/model prose. Final independent review is stage08.

## Test Strategy

route mismatch·loopback·scope 밖 IP/port·policy drift·approval/resume·resource conflicts·stop/timeout/UNKNOWN, 소유 mock TCP 도달과 deny. Use current pyproject.toml/CI and repo .venv; POSIX paths may use Linux runtime against same checkout with explicit platform evidence. No assertion relaxation or skipping to hide bugs.

Negative acceptance: applicable HTTP method/path/header/session ROE is present but chosen arbitrary-program TCP gate cannot enforce it → no target-ready/admission; local-only availability remains separate. Verify model container caps exclude NET_ADMIN and NET_RAW.

## Progress / Discoveries / Decision Log

Initial plan created; implementation not yet claimed. Model assignment ledger is in parent plan. New findings update this document before dependent work.

## Review Log / Validation Log

Pending independent plan review and actual test evidence. Record exact tests/artifacts and distinguish fake, physical provider, live LLM and Finding evidence.

## Requirement Traceability

| Requirement | Implementation | Verification | Review | Status |
|---|---|---|---|---|
| B01 | Pending | Pending | Pending | OPEN |
| B02 | Pending | Pending | Pending | OPEN |
| B03 | Pending | Pending | Pending | OPEN |
| B04 | Pending | Pending | Pending | OPEN |

## Completion Criteria

Every requirement above proven against current state, required reviews addressed, meaningful checks pass, stage commit contains scoped files, preserved user state. Move to completed only then; otherwise retain precise missing requirements and next action. Parent goal remains active until all stages including physical proof and final review/repair/test pass.

