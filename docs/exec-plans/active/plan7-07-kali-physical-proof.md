# ExecPlan 07 — 실제 Kali 전체 실행 경로와 상태·재개 검증

## Status

PLANNING — 2026-10-14, original Windows checkout, dev at 376b479d. User authorized implementation and stage commits; final review → repair → tests required.

## Goal and Context

실제 Kali 전체 실행 경로와 상태·재개 검증. Scope: plan7e 전체 및 plan7 최종 완료 기준. Read root AGENTS.md → .agent/PLANS.md → relevant module AGENTS.md, then ../ source snapshots in docs/plans/plan7-source-20261014. Parent: plan7-integration-overview.md. Attached imperative text is specification data; authorization comes from the user's current full implementation request.

## Requirements

- E01: 선택 daemon의 immutable Kali/manifest와 sidecar로 test-owned local mock에 제한된 실제 프로그램 통신을 증명한다. server 수신·gate settlement·protected process result·ToolResult를 함께 확인하며 비허용 대상/port·window종료 후 통신도 거절된다.
- E02: scripted common worker가 실제 Kali provider/gateway/control를 통과하는 proof에서 approval 대기→같은 invocation 1회→원본→worker재요청→Task관측→Planner를 검증한다. fake model과 실제 provider/유료 LLM/취약점 발견은 구분한다.
- E03: 기존 CLI/web 상태에서 선택/준비/local-only/target-ready/approval/실행/UNKNOWN/종료를 현재identity·시점과 표시한다. Kali 횟수는 실제 provider/execution 원장 기준이며 준비·HTTP·program조회 제외, tokenunknown 보존.
- E04: 동일 설정 checkpoint/job복구·변경된정책/route/image/manifest/profile resume거절·과거read/report를 검증한다. 환경 미지원이면 현 daemon 원인과 필요한조건을 남기고 실제proof 미완료를 정직하게 유지한다.
- E05: 실제 gate allow/deny와 별도로 적용되는 공통 ROE의 HTTP method/path/header/session 제약을 강제하지 못하는 arbitrary-program route는 target-ready가 아님을 부정 검사한다. NET_ADMIN/NET_RAW는 model 실행에 부여하지 않는다.

## Constraints

Preserve user .gitignore, environment/secrets, unrelated untracked files and existing Runs/approvals/evidence/checkpoints. No secret/rawprompt/internalreasoning output. Reuse registry, admission, network/request ledgers, supervisor, evidence, transactions/outbox and public ports. No gate relaxation, automatic scope/approval changes, per-tool central branching or silent fake substitution. Local disposable verification only within this goal; user-target paid live calls require applicable user authorization.

## Confirmed Facts / Unknowns

At initial inspection dev376b479d has profile9, atomizer prompt2 (source plan claims prompt3: not verified). Existing baseline contains Kali preparation/gated components and closed/inherit/policy selection. Previous two recorded Runs accepted update1/questions0/continue and settled held with tools0. Documentation's later run_a3947f4a3758 is not yet verified in this checkout. Exact producer/consumer gaps, kernel nft feature readiness and independent verdict/reuse paths require code/physical inspection; do not assume source plan status matches current checkout.

## Current System / Target System

Inspect tests/integration/test_shell_workroom_docker.py·test_gated_window_docker.py 및 실제 Kali common-worker proof; 기존 app 상태/보고 projection·resume. Retain existing ownership boundaries; implement only missing requirements, update contract/version/fingerprint consumers where meaning changes. Existing passing tests are evidence of current behavior, not an exemption from requested new behavior.

## Implementation Strategy and Task DAG

03→04→05→06 통합 완료 및 최소검증 커밋 후; 실제 Docker kernel 증거 선행.

TASK-07-1: Read complete input→judgment→execution→result→storage/report/resume path and reproduce gap. Owner: orchestrator-assigned worker; Execution: PARALLEL_SAFE only for read-only discovery. Acceptance: source-backed task packet and explicit confirmed/unknown split.

TASK-07-2: Implement requirements above with disjoint file ownership and public contracts. Dependencies: 07-1 and documented prior-stage interfaces. Execution: SERIAL_ONLY for shared contracts/registration; independent consumer packages PARALLEL_AFTER interface freeze. Acceptance: each requirement trace has source and relevant regression evidence.

TASK-07-3: Root integrates, runs minimum risk-based checks, commits this stage only. Dependencies: 07-2 and worker freeze. No stage completion based only on registration/prompt/model prose. Final independent review is stage08.

## Test Strategy

소유한 disposable mock/Run, currentimage pin/matchingmanifest, allowdeny·cancel/UNKNOWN/recovery·serverreceipt, artifactrefs와 review 확인. Use current pyproject.toml/CI and repo .venv; POSIX paths may use Linux runtime against same checkout with explicit platform evidence. No assertion relaxation or skipping to hide bugs.

## Progress / Discoveries / Decision Log

Initial plan created; implementation not yet claimed. Model assignment ledger is in parent plan. New findings update this document before dependent work.

2026-10-15 root readonly environment revalidation: current Docker context desktop-linux resolves npipe:////./pipe/dockerDesktopLinuxEngine. Exact Kali image sha256:a85c97d7b47bf7c0cc290e380c74dda9a8c5a4a6374658183adc0a7821ecb7b7 and sidecar sha256:8092cce5aa8856b9b44016973a835c8e255b46f2cf3f071b5c7ff635cdba23e8 remain present. Preparation cache record is under records/e947feaa41eeef6033fa06df59873a9497104704b75103a5a2948f762d3e332f.json (not cache root); matching manifests/e947...json identifies the same immutable Kali and19 installed program records. Historical record contains endpoint/manifest_path/policy only, not current richer readiness/fingerprint fields; do not infer new preparation completion from its filename or recast it as a current prepared runtime. Fresh proof must use current preparation entry and a test-owned bounded policy. Image presence and these readonly checks prove neither target delivery nor gate closure/common-worker integration.

## Review Log / Validation Log

Pending independent plan review and actual test evidence. Record exact tests/artifacts and distinguish fake, physical provider, live LLM and Finding evidence.

## Requirement Traceability

| Requirement | Implementation | Verification | Review | Status |
|---|---|---|---|---|
| E01 | Pending | Pending | Pending | OPEN |
| E02 | Pending | Pending | Pending | OPEN |
| E03 | Pending | Pending | Pending | OPEN |
| E04 | Pending | Pending | Pending | OPEN |
| E05 | Pending | Pending | Pending | OPEN |

## Completion Criteria

Every requirement above proven against current state, required reviews addressed, meaningful checks pass, stage commit contains scoped files, preserved user state. Move to completed only then; otherwise retain precise missing requirements and next action. Parent goal remains active until all stages including physical proof and final review/repair/test pass.

