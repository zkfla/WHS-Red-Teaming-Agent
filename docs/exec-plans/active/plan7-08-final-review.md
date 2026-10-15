# ExecPlan 08 — 최종 독립 리뷰·수정·테스트

## Latest user decision — direct target execution (2026-10-15)

The user explicitly selected the teammate's direct-program provider as the preferred default and authorized adapting the plans and importing that implementation with concrete bug fixes. This decision supersedes earlier acceptance requirements that mandate a packet gate, sidecar/NFQUEUE, byte/flow network ledger, or deny target readiness solely because arbitrary programs cannot enforce HTTP method/path/header/session restrictions. Original source plan snapshots remain historical design data, not an override of this decision.

The integration target is origin/dev 38179561 direct_target / prepare_kali_network / production bootstrap, preserving verified planner, atomizer, structured diagnostic, original-evidence and completion fixes. Validate actual selected Docker endpoint/bridge identity, declared target/port and explicit scope/deny admission, ordinary human approval, execution request/time/output budgets, same-instance exclusion, cancellation/timeout and network detach, protected original results, truthful UNKNOWN, and configuration-bound resume. Target-ready means measured direct-route readiness and must retain truthful network_enforcement=none; it must not claim packet isolation or per-request HTTP enforcement. No model NET_ADMIN/NET_RAW or automatic scope expansion is introduced by this choice.

Physical acceptance now exercises the actual direct provider through the production assembly/common-worker path against an explicitly owned local fixture, including admission refusal, delivery, timeout/cancellation/detach and stored result/report/resume. Old gated-wire deny and verified gate-settlement tests are not prerequisites for the new default. Existing dormant gate code and unfinished gate-specific repair are preserved at a safe checkpoint; they are not claimed complete or silently activated. Do not add a new alternate-mode framework merely to retain the abandoned design.

Import is staged after current writers freeze, preserving unrelated local files and staged boundaries. Review teammate code for concrete errors rather than rejecting its chosen architecture. Reconcile overlapping planner/context/provenance changes and avoid duplicate descriptor registration. Re-run applicable checks after integration and retain the final review/repair/test/commit/push requirements. The earlier instruction to postpone remote inspection/integration is superseded to the extent required to bring in the implementation the user now selected.


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

### 2026-10-15 integrated review and repair checkpoint

Independent Sol reviewed the retained progress, admission diagnostics, source reuse, context-root capture, direct detach and read-only startup validation. Auxiliary agy/Gemini final review completed (job1792035226578649100-f6eb1b2d55f16c1ffc26b3d9). Concrete cache-loss false detach and empty context-root issues were accepted and repaired; a proposed closed-Run network bypass was rejected because canonical selection binding refuses before actual attachment. A separate explicit migration adapter defect was repaired: historical schema2 identity is reconstructed only for legacy schema4 source validation, while the native binding remains current. Candidate policy/substantive drift and entered executions remain refused. Sol accepted the narrow repair; root16 migration regressions passed.

Root merged-state evidence:33 transport/config/target/context tests;25 conversation/transport/completion diagnostic tests;30 progress/reuse/web projection tests;32 central CLI/resume/collect/process tests; worker38 recovery/environment/preparation tests. Full source/test lint passed before the latest actively edited physical fixture. Source/test mypy passed before that new fixture; its remaining annotation is assigned. Both compose contracts pass. Earlier full suite4477PASS/132SKIP/25FAIL is retained as a failing baseline; targeted repairs do not claim a new full-suite PASS. PTY and two intermediate-planner regressions are still being repaired/rechecked.

Actual Cat Jam High bank run executed2 GETs and stopped at high-risk approval; another run exhausted planner correction attempts. Neither establishes full analysis completion. Physical direct gateway proof reached an owned mock and detached, but the production assembly/Control/common-worker physical chain is not yet passing. F02/F03 therefore remain OPEN; final push and remote reconciliation remain pending. Parent integration plan contains the source commits and detailed dispositions.

## Requirement Traceability

| Requirement | Implementation | Verification | Review | Status |
|---|---|---|---|---|
| F01 | Sol source reviews + agy auxiliary review; concrete repairs reconciled | Integrated verification report and scoped review rechecks | CLEAR | PASS |
| F02 | CI-path Ruff/mypy/compose; physical direct proof and live bank runs | Full4510 PASS/133 SKIP/2 failures reconciled by isolated2 PASS; targeted late fixes PASS | Scope and limitations documented | PASS |
| F03 | Pending | Pending | Pending | OPEN |

## Completion Criteria

Every requirement above proven against current state, required reviews addressed, meaningful checks pass, stage commit contains scoped files, preserved user state. Move to completed only then; otherwise retain precise missing requirements and next action. Parent goal remains active until all stages including physical proof and final review/repair/test pass.

