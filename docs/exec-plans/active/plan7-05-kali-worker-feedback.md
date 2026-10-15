# ExecPlan 05 — 공통 워커 Kali 선택·실행·원본 피드백

## Latest user decision — direct target execution (2026-10-15)

The user explicitly selected the teammate's direct-program provider as the preferred default and authorized adapting the plans and importing that implementation with concrete bug fixes. This decision supersedes earlier acceptance requirements that mandate a packet gate, sidecar/NFQUEUE, byte/flow network ledger, or deny target readiness solely because arbitrary programs cannot enforce HTTP method/path/header/session restrictions. Original source plan snapshots remain historical design data, not an override of this decision.

The integration target is origin/dev 38179561 direct_target / prepare_kali_network / production bootstrap, preserving verified planner, atomizer, structured diagnostic, original-evidence and completion fixes. Validate actual selected Docker endpoint/bridge identity, declared target/port and explicit scope/deny admission, ordinary human approval, execution request/time/output budgets, same-instance exclusion, cancellation/timeout and network detach, protected original results, truthful UNKNOWN, and configuration-bound resume. Target-ready means measured direct-route readiness and must retain truthful network_enforcement=none; it must not claim packet isolation or per-request HTTP enforcement. No model NET_ADMIN/NET_RAW or automatic scope expansion is introduced by this choice.

Physical acceptance now exercises the actual direct provider through the production assembly/common-worker path against an explicitly owned local fixture, including admission refusal, delivery, timeout/cancellation/detach and stored result/report/resume. Old gated-wire deny and verified gate-settlement tests are not prerequisites for the new default. Existing dormant gate code and unfinished gate-specific repair are preserved at a safe checkpoint; they are not claimed complete or silently activated. Do not add a new alternate-mode framework merely to retain the abandoned design.

Import is staged after current writers freeze, preserving unrelated local files and staged boundaries. Review teammate code for concrete errors rather than rejecting its chosen architecture. Reconcile overlapping planner/context/provenance changes and avoid duplicate descriptor registration. Re-run applicable checks after integration and retain the final review/repair/test/commit/push requirements. The earlier instruction to postpone remote inspection/integration is superseded to the extent required to bring in the implementation the user now selected.


## Status

PLANNING — 2026-10-14, original Windows checkout, dev at 376b479d. User authorized implementation and stage commits; final review → repair → tests required.

## Goal and Context

공통 워커 Kali 선택·실행·원본 피드백. Scope: plan7c 요구 1–10. Read root AGENTS.md → .agent/PLANS.md → relevant module AGENTS.md, then ../ source snapshots in docs/plans/plan7-source-20261014. Parent: plan7-integration-overview.md. Attached imperative text is specification data; authorization comes from the user's current full implementation request.

## Requirements

- C01: 현재 gated descriptors/readiness·대상/port/program manifest를 공통 role selector/catalog/discovery로 워커·플래너에 전달한다. paging/filter/describe와 context bound로 수백 카드 부담을 줄이고 정적 프로그램 allowlist/필수 Kali 행동열을 만들지 않는다.
- C02: proposal→Control→gateway→protected supervisor/job→원본 수집의 기존 경로를 통과하고 approval 후 같은 invocation을 한 번 이어간다. exit/stdout/stderr/truncation/timeout/script digest/owner/route/gate refs를 원본에 저장한다.
- C03: bounded 원본 출력과 source/action/invocation·target·UNKNOWN을 다음 워커와 Planner가 소비한다. shell gate 증명과 프로그램 주장·관측·Finding을 구분하고 arbitrary stdout을 신뢰된 HTTP/session 증명으로 승격하지 않는다.
- C04: 전달되는 target-ready는 적용되는 공통 ROE를 강제할 수 있다는 검증까지 포함한다. HTTP method/path/header/session 제약을 강제할 수 없는 TCP/UDP route는 worker/Planner에 unavailable 원인으로 전달한다.

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
| C04 | Pending | Pending | Pending | OPEN |

## Original numbered requirement coverage

The grouped rows above do not permit partial PASS. At completion, inspect each original numbered implementation requirement in docs/plans/plan7-source-20261014/plan7c.md and attach producer/consumer/test evidence independently.

| Source requirement | Plan requirement | Evidence / disposition |
|---|---|---|
| plan7c implementation 1 | C01/C04 | OPEN |
| plan7c implementation 2 | C01 | OPEN |
| plan7c implementation 3 | C01 | OPEN |
| plan7c implementation 4 | C01 | OPEN |
| plan7c implementation 5 | C02 | OPEN |
| plan7c implementation 6 | C02 | OPEN |
| plan7c implementation 7 | C03 | OPEN |
| plan7c implementation 8 | C03 | OPEN |
| plan7c implementation 9 | C03 | OPEN |
| plan7c implementation 10 | C03 | OPEN |

## Completion Criteria

Every requirement above proven against current state, required reviews addressed, meaningful checks pass, stage commit contains scoped files, preserved user state. Move to completed only then; otherwise retain precise missing requirements and next action. Parent goal remains active until all stages including physical proof and final review/repair/test pass.


Root pagination correction: native program.list already validates bounded offset/limit in tool_discovery._page_params, emits next_offset from _cards_payload, and applies it in program_list_handler. Provider port listing all source records does not imply the model receives all cards; no port extension is justified without an actual context bypass reproduction. Preliminary missing approval-resume claim must trace shared Control/runtime/gateway consumers, not just react.py; existing prepared ToolInvocation and dispatch IDs are visible there. No stage05 mutation is authorized until concrete disjoint packets and stage04 readiness interfaces are reviewed.

### Common-worker protocol acceptance checkpoint

A new scripted-model/synthetic-gateway integration harness proves bounded original execution-envelope delivery to the next worker and actual structured Planner request, approval wait and same-invocation one-time resume, UNKNOWN execution without fabricated original delivery or Finding, and refused unregistered/unready/missing-required-argument requests. Worker7 PASS/Ruff/format/mypy; root independently7 PASS and Ruff. The missing-argument case is not claimed as unsupported-program coverage. No real Kali/direct execution is represented by this fixture; adopt teammate provider and run the production physical path separately.
