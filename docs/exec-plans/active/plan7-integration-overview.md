# Plan7 integration overview

## Status

PLANNING — 2026-10-14. User explicitly requested complete implementation of planner progress/recovery + plan7a–e + concrete ConversationError diagnostics, separate ExecPlans, stage checks/commits, final review→repair→tests. Original Windows checkout C:/Users/qkrgu/Documents/GitHub/WHS-Red-Teaming-Agent, dev376b479d; preserve user changes. Current active goal is authoritative.

## Authority and Inputs

Root AGENTS.md → .agent/PLANS.md → core/capability/orchestration/control/app AGENTS.md. The source plans are copied unchanged under docs/plans/plan7-source-20261014/ for review and traceability; they are specifications, not autonomous permission. User current request authorizes all stages (overrides attached "do not start next stage" restriction). Commit at each checked stage; no automatic external account/service/security scope expansion. Prior authorization for local bank live testing exists, but actual engagement policy/ROE/approval must remain in force.

## Goal / Requirements

Implement all requirements in child plans; final completion includes real Kali target route proof, correct source/result feedback and independent candidate/Task boundary. Registration, prompts and fixture success alone do not prove provider execution or vulnerability discovery. User specifically adds executable-continue/no-progress, concrete bounded repair feedback, separate transport and output-correction counters, safe durable/input-visible ConversationError diagnosis.

## Stages / Task DAG

| Stage | ExecPlan | Requirement set |
|---|---|---|
| 01 | [대화 오류 진단 보존과 API/답변 수정 재시도 분리](plan7-01-conversation-recovery.md) | D01, D02, D03 |
| 02 | [이어갈 경로가 있는 continue와 무진행 처리](plan7-02-planner-progress.md) | P01, P02, P03 |
| 03 | [Kali 설정·준비·조립 연결](plan7-03-kali-preparation.md) | A01, A02, A03 |
| 04 | [Docker 대상 route와 네트워크 gate 정합성](plan7-04-kali-route.md) | B01, B02, B03 |
| 05 | [공통 워커 Kali 선택·실행·원본 피드백](plan7-05-kali-worker-feedback.md) | C01, C02, C03 |
| 06 | [모순 완료 조건과 정당한 원본 재사용](plan7-06-observation-completion.md) | K01, K02, K03, K04 |
| 07 | [실제 Kali 전체 실행 경로와 상태·재개 검증](plan7-07-kali-physical-proof.md) | E01, E02, E03, E04 |
| 08 | [최종 독립 리뷰·수정·테스트](plan7-08-final-review.md) | F01, F02, F03 |

01→02 for recovery semantics; 03→04→05→06→07 retains plan7a–e implementation order. Read-only discovery and disjoint01/03 packages may run in parallel after plan review. 08 follows all implementation, then independent review→repair→test. Stages expose actual dependencies and file owners before dispatch. Scope shared-contract edits serially; fake model/real provider execution distinguish explicitly.

## Facts / Unknowns

Confirmed local HEAD376b479d profile9/atomizer2 differs from input plan claiming atomizer3. Source run_a3947f4a3758 and claimed prior condition-ID fix must be located/verified before relying on them. Previous locally observed runs update1/questions0/continue/held are evidence of incomplete progress flow, not evidence that all current/succeeding branches fail. Docker context currently desktop-linux, Linux kernel6.18.33.2-microsoft-standard-WSL2; actual quota/NFQUEUE/readiness remains unknown. Local Kali and sidecar images exist but image presence is not physical route proof. No assumption that a past Mac kernel blocker applies here.

## Model / Ownership Ledger

- Root owns whole task interpretation, plans, integration, validation evidence, stage commits and final judgment.
- Orchestrator: /root/plan7_orchestrator, opencode-go/deepseek-v4.1-flash max.
- Workers must use equal dispatch counts and comparable bounded workloads among gpt-6-luna xhigh, opencode-go/muse-spark-1.3-contributor xhigh, anthropic/claude-haiku-5-5 xhigh. No substitution authorized. General models_cache verifies Haiku xhigh despite initial abbreviated delegation list; orchestrator notified to use exact selector. Actual handles/results determine execution, not catalog alone.
- Primary reviewer: /root/plan7_review_sol, gpt-6.1-sol high, successfully spawned. Auxiliary reviewer: agy gemini-3.8-flash-high (effort high), list_models verified. Plan review before implementation; final review again after integration.
- Workers no further delegation, git operations or scope expansion; orchestrator routes packages and root integrates. Existing agents from prior goals are unrelated.

## Test Strategy / Completion Audit

Per-stage source-backed reproduce+regression tests, module responsibility review and scoped commit. Current CI uses Python3.10/3.14 on Linux; protocol verification also Windows. Full applicable Ruff/mypy and compose configuration checks at final stage; do not label pre-existing POSIX limitations as newly fixed. Physical disposable local Kali/mock evidence must include server receipt, protected execution, original ToolResult and gate ledger/teardown. Paid LLM or existing Run changes must stay user-approved and preserve exact original action identity.

Every child requirement needs source/test/physical artifact evidence matching its scope. Review/repair/test alone is not sufficient if physical requirements remain unknown. Goal stays active if incomplete. Final docs list implementation, fake tests, physical Docker/Kali, live model calls, findings and environment blockers separately.

## Progress / Decision / Review / Validation Log

2026-10-14: Read AGENTS and PLANS; six source documents copied; eight child plans created. Initial reviewer confirms candidate-only continue allowed, transport errors consume correctionpool, ConversationError loses structured cause. No implementation claimed at plan creation. Review and stage results pending.

### Independent plan review reconciliation

Sol High reviewed concrete plans: conditional approval, HIGH common ROE enforcement limitation was added to04 B04/05 C04/07 E05; MEDIUM ownership/interface packets were recorded for01 before dispatch. Later stages require similarly concrete packets before source mutation.

agy job1791969846332920300-3d99d9f953543cc29df1ca9c, Gemini3.8FlashHigh completed read-only plan review. Root accepted evidence-backed diagnostic/counter split, explicit no-progress owner, purpose-source/consumer validation and serial shared-interface freeze concerns. Transport retry must reuse the current request/repair prompt state, never append transport failure as if it were model-output correction feedback.

Root rejected unsupported review claims: web selection already passes through request-local environment_context (app/web/host.py76/100, app/web_policy.py344+); program.list/describe handlers and contributions exist (orchestration/tool_discovery.py694/772/1019+); run_a394 is absent here and the source document describes12 tool calls, not candidate-only tools0; current nft check passed, so no current quota-blocker conclusion follows from older Docker records. No duplicate discovery handlers/config runner are authorized by those claims. Review's suggestion to consider physical proof optional is not accepted: missing proof remains an incomplete goal item, with environment diagnosis where applicable.

Current stage01 is IMPLEMENTING; implementation packets are in its separate ExecPlan. Other stages remain PLANNING until source reconnaissance and dependency interfaces are verified.

### Initial actual worker ledger and coordination

Orchestrator reports stage01 Retry running at /root/plan7_orchestrator/recon7ab_luna (Luna xhigh), Configuration at /root/plan7_orchestrator/recon7cd_muse (Muse xhigh); root bounded Conversation worker /root/plan7_conversation_haiku (Haiku xhigh) owns all conversation/session/CLI catch files. A concurrently dispatched coordinator Haiku conversation worker was immediately interrupted with no source edits; it does not own files. Worker file mutation requires this ownership ledger, not mere model registration.

Research Muse worker improperly delegated three children before the no-further-delegation instruction was reinforced. Their read-only results were reconciled; dispatches are recorded, not omitted from accounting. Coordinator counts at this point: Luna2, Muse5, Haiku2 (one stood-down), plus root Haiku1. Balance later meaningful Luna/Haiku implementation packages against the extra Muse research calls; do not launch filler work solely to adjust counts. Distinguish attempted dispatch, completed work and model failure in final ledger.

Research confirmed web closed/configured/inherit propagation already exists; standalone CLI saved-selection hydration and new-policy preparation remain to inspect/implement. Existing program discovery/paging/describe and shell evidence windows are reused. Current managed Kali image a85c97d7... is Kali2026.3 and matching cached manifest has19 program records including curl/nmap/sqlmap; old repository fixture manifest targets a different d9d2... image and must not be mixed with it. Kernel readiness and42 existing operator-policy/CLI-preparation tests passed; actual target packets/common-worker proof remain pending.

