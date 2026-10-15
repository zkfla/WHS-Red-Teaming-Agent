# ExecPlan 07 — 실제 Kali 전체 실행 경로와 상태·재개 검증

## Latest user decision — direct target execution (2026-10-15)

The user explicitly selected the teammate's direct-program provider as the preferred default and authorized adapting the plans and importing that implementation with concrete bug fixes. This decision supersedes earlier acceptance requirements that mandate a packet gate, sidecar/NFQUEUE, byte/flow network ledger, or deny target readiness solely because arbitrary programs cannot enforce HTTP method/path/header/session restrictions. Original source plan snapshots remain historical design data, not an override of this decision.

The integration target is origin/dev 38179561 direct_target / prepare_kali_network / production bootstrap, preserving verified planner, atomizer, structured diagnostic, original-evidence and completion fixes. Validate actual selected Docker endpoint/bridge identity, declared target/port and explicit scope/deny admission, ordinary human approval, execution request/time/output budgets, same-instance exclusion, cancellation/timeout and network detach, protected original results, truthful UNKNOWN, and configuration-bound resume. Target-ready means measured direct-route readiness and must retain truthful network_enforcement=none; it must not claim packet isolation or per-request HTTP enforcement. No model NET_ADMIN/NET_RAW or automatic scope expansion is introduced by this choice.

Physical acceptance now exercises the actual direct provider through the production assembly/common-worker path against an explicitly owned local fixture, including admission refusal, delivery, timeout/cancellation/detach and stored result/report/resume. Old gated-wire deny and verified gate-settlement tests are not prerequisites for the new default. Existing dormant gate code and unfinished gate-specific repair are preserved at a safe checkpoint; they are not claimed complete or silently activated. Do not add a new alternate-mode framework merely to retain the abandoned design.

Import is staged after current writers freeze, preserving unrelated local files and staged boundaries. Review teammate code for concrete errors rather than rejecting its chosen architecture. Reconcile overlapping planner/context/provenance changes and avoid duplicate descriptor registration. Re-run applicable checks after integration and retain the final review/repair/test/commit/push requirements. The earlier instruction to postpone remote inspection/integration is superseded to the extent required to bring in the implementation the user now selected.


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

### Concrete proof packets (proposal; independent review before mutation/execution)

TASK-07-P — Physical provider/window proof. Own a new isolated integration proof and reuse tests/integration/fixtures/gated_network.py or another test-owned bounded server fixture; do not rewrite existing broad Docker proofs to hide their failures. Inputs are explicit selected Docker endpoint, immutable Kali/sidecar and current matching manifest supplied by current preparation. Create only uniquely named test-owned internal network/mock/container/Run resources; no existing bank/Run, global Docker context or policies are changed. Logical target and literal pins come from the fixture's actual inspected deployment and explicit synthetic scope, with a concrete selected port and finite capacities. Invoke the real protected Kali provider through its public assembly/gateway/Control contract, record mock server reception, original ToolResult/protected process and same window settlement. Exercise allowed communication, a nonselected port/target refusal and closure-before-next-communication denial. Test names and inputs may choose a bounded installed program; ordinary application behavior gains no forced command sequence. Unknown meter usage remains UNKNOWN/full charged under the owner contract, never fabricated zero. Assert execution container lacks NET_ADMIN/NET_RAW; sidecar privilege is not transferred. Cleanup verifies exact test-owned names/labels; uncertain ownership remains reported, never broad prune. Dependencies:03 preparation and04 cleanup/ROE interfaces accepted; this packet is not yet authorized for source mutation or physical execution.

TASK-07-W — Actual common-worker consuming chain. Reuse the independently reviewed05 scripted-model/common-worker harness after its interface freeze, substituting the real prepared Kali provider and test-owned mock from07-P, not replacing Control/gateway/approval/supervisor/evidence/Task/Planner with direct calls. Assert approval waiting and original invocation resumes exactly once, bounded original output/gate references delivered in the next worker request and actual Planner payload, observation Task review and follow-up criterion-ID restoration through06. Server receipt and provider/gate originals prove transmission; exit0/stdout claims alone do not prove remote facts, candidate confirmation, Finding or goal success. Model fake, real Docker provider, actual paid LLM and vulnerability findings must be reported separately. Include only relevant same-instance conflict, cancellation/timeout/UNKNOWN/original recovery negatives from the existing owner tests; no quota reset or quarantine refund to pass. Apply applicable HTTP restriction negative through actual04-R guard before ledger reservation and verify UI/CLI readiness remains unavailable. Dependencies:07-P,05 chain,06 producer/ID/reuse freeze; no fake-backed physical claim.

TASK-07-I — Identity/status acceptance. Reuse current CLI/web projections and original execution/request ledgers, verify prepared endpoint/image/policy/manifest/readiness timestamp and actual Kali execution counts (exclude preparation/discovery/native HTTP), preserve unknown token usage. Same identity resumes original checkpoint/jobs; changed policy/route/image/manifest/profile refuses. Root records actual source/test/artifact evidence per E01–E05 after the two proof packets, with any concrete integration gap handed to its original owner. This is not a new UI state store or central server-name count list. No overall stage PASS from a smaller readiness check.

Independent07 plan reviewer concrete topology finding accepted after root source inspection: public GatedExecutionPolicy.execute chooses a fresh epoch and open_window(internal=False), creating its own nft-controlled per-window bridge; older Docker tests precreate the same known epoch1 bridge and call the extension directly. Their internal-network fixture cannot be substituted into the actual gateway/common-worker proof unchanged. Amend07-P's internal-only resource phrase: the original provider-created, owner-labelled nft-controlled bridge is explicitly allowed; freeze a test-owned mock endpoint genuinely reachable through that original topology before implementation. Do not patch clocks/epoch/internal/resolve, manually open the grant or loosen gate policy to make the proof pass. Current P topology remains unproven until this exact fixture route is reviewed and then measured. A pre-admission target/port refusal proves admission only; separate bounded denied wire attempts and post-closure attempts require unchanged mock reception plus protected process/gate evidence to prove packet isolation. No physical execution authorized by this plan correction alone.

07 reviewed fixture interface refinement: a test-only OwnedMockEndpoint carries logical_host, actual literal_ipv4, effective port, tcp transport, original DockerEndpoint, exact owned_resource_ids and receipt_artifact path. Existing provider-created nft-controlled bridge remains unchanged; determine the daemon-host-reachable published mock endpoint by a separate bounded fixture baseline, never use the named-pipe management HOST as a TCP address or assume Windows loopback/host-gateway reachability. Baseline establishes topology only and is excluded from gateway execution counts. Actual operator binding/synthetic scope, registry pins and selected endpoint derive from this verified fixture; current preparation/public application assembly and actual04R applicability guard still apply. Do not substitute an empty restriction projection to permit raw TCP. This fixture dataclass is test scaffolding, not a new product contract/store.

07 physical negative acceptance is explicitly split. Admission: wrong selected target/port refuses before provider execution/window reservation. Wire: within one original authorized protected-program invocation/window, bounded attempts reach the allowed endpoint but not a second existing mock target or a nonallowed open listener port; correlate each protected result with mock receipt changes, not a missing-listener connection error. Closure: after original settlement, a bounded local-provider probe in the SAME workroom without opening another gate cannot reach the old mock; retain protected original/companion and unchanged server receipt. Verify this with actual provider/gateway contracts, no direct arbitrary docker exec substituting the requested path. A new gated invocation opens a new window and cannot prove old-window closure. RELEASED with verified closure is required for positive closure; UNKNOWN/quarantine is preserved full-charged and reported incomplete, never accepted via a RELEASED-or-QUARANTINED assertion. Root accepts reviewer HIGH topology and MEDIUM physical-negative findings; corrected packet awaits fresh plan recheck.

Fresh independent Sol07 plan recheck CLEAR after these corrections. This clears packet design only, not measured topology, physical delivery/closure, common-worker consumption or stage completion. Source/proof execution follows named predecessor interface acceptance and exact test-owned fixture checkpoint; no global context/Run/quota mutation or fake physical substitution.

User live-model selection2026-10-15: actual LLM live tests use Cat Jam Free with High effort; coding worker/coordinator assignments are unaffected. Official OpenCode Go model documentation https://dev.opencode.ai/v2/docs/console/go/ lists wire model cat-jam-free with OpenAI-compatible chat/completions (current advertised endpoint https://opencode.ai/inference/go/v1/chat/completions). This repository's opencode-go selector uses openai/cat-jam-free for CHAT, not openai/responses/ or anthropic/, with reasoning_effort=high. Existing adapter still targets its legacy /zen/go/v1 base; verify actual bounded preflight on the selected client before live target testing rather than assuming alias availability or silently changing global endpoint/config. High transmission/acceptance remains a live check, not inferred solely from model catalog. Use isolated test configuration/Run, preserve existing user settings and never print API keys; actual model/provider/request/effort and diagnostics are recorded safely. Physical scripted-model proof remains explicitly separate from this later live model verification.

## Review Log / Validation Log

Pending independent plan review and actual test evidence. Record exact tests/artifacts and distinguish fake, physical provider, live LLM and Finding evidence.

### Actual direct production chain proof (2026-10-15)

Commitcf059163 adds two opt-in owned-resource Docker tests. The gateway route fixture verifies immutable local Kali preparation, a local internal bridge, actual mock receipt (one ACCEPT/12bytes), stdout and detach, scope refusal and no model NET_ADMIN/NET_RAW. The production-chain fixture uses real open_central_run→build_application(direct_target_shell), real Control with an explicit medium approval threshold, scripted model tool selection, operator resolution, same invocation recovery, one successful physical execution, protected TOOL evidence and Task result, worker follow-up, and actual Planner original-observation consumption. Root final production-chain run PASS in29.36seconds; owned container/network label inventory is empty afterward. Models in this proof are explicitly scripted, not paid LLMs, and the mock response is not a verified Finding.

The fixture initially used the unrelated unit.local Task target and was correctly refused, then used an action budget of1 and was auto-settled at the execution boundary. The final fixture binds its Task to the explicit owned mock target and reserves2 actions so a worker follow-up can occur; actual execution count remains1. No production scope, Control, evidence gate or runtime budget was weakened to make the proof pass. Planner output projection carries exit status under execution.accounting, while stdout remains program_output.

Root is extending the route fixture with actual timeout/cancellation detach checks and an exact unselected-port rejection assertion. These checks are not claimed passed before their run finishes. Final integrated suite and final review/remote reconciliation remain required in08.

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

