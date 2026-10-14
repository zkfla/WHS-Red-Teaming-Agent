# ExecPlan 04 — Docker 대상 route와 네트워크 gate 정합성

## Status

IMPLEMENTING — 2026-10-14, original Windows checkout, dev at 376b479d. User authorized implementation and stage commits; final review → repair → tests required.

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

## Original numbered requirement coverage

The grouped rows above do not permit partial PASS. At completion, inspect each original numbered implementation requirement in docs/plans/plan7-source-20261014/plan7b.md and attach producer/consumer/test evidence independently.

| Source requirement | Plan requirement | Evidence / disposition |
|---|---|---|
| plan7b implementation 1 | B02 | OPEN |
| plan7b implementation 2 | B01 | OPEN |
| plan7b implementation 3 | B01 | OPEN |
| plan7b implementation 4 | B01 | OPEN |
| plan7b implementation 5 | B02 | OPEN |
| plan7b implementation 6 | B02 | OPEN |
| plan7b implementation 7 | B04 | OPEN |
| plan7b implementation 8 | B03 | OPEN |

## Completion Criteria

Every requirement above proven against current state, required reviews addressed, meaningful checks pass, stage commit contains scoped files, preserved user state. Move to completed only then; otherwise retain precise missing requirements and next action. Parent goal remains active until all stages including physical proof and final review/repair/test pass.


Root route inspection: configure_gated currently injects only check_target_scope over EngagementScope; it does not consume ServerPolicy. WebPolicySelection jointly records request permissions (read_only/custom/full), method/path/query/header/body/session choices and Kali network selection; DeploymentSpec produces bound ServerPolicy and public metadata. Therefore applicability must be traced through the selected engagement/deployment rather than treating every unrelated MCP server policy as global ROE, or silently treating the form restrictions as irrelevant. A packet gate cannot certify HTTP semantics; full HTTP selection is still not universal arbitrary-byte transport authority. Concrete route/admission packet remains pending coordinator source-backed applicability decision and independent plan review.

Root D1 source decision: check_target_scope explicitly permits a trusted address-pin consumer to positive-check the approved registry name and separately apply literal-address denials. Requiring every pin to pass a second positive allow-list would change the chosen scope contract and is rejected. Preserve logical name authority, explicit denials for all pins and operator pin-binding identity; route request cannot invent pins or scope. D6 legacy policies retain existing broad behavior while new builder requires endpoint selection; D7 request-local inherit remains unless a concrete bug is demonstrated. Preliminary recon naming/cancellation concerns require injected reproductions; loopback blanket rejection, override bypass and optional new location enum are not approved. Existing indirect central-selection identity enforcement remains authoritative.

Independent Sol bounded reproductions accepted (actual SQLite ledger + injected Docker runner; no physical traffic): cancellation after simulated network transition and verification failure followed by foreign-label cleanup rejection both leave _opening held, no _open recoverable handle, ledger open/no settlement, retry busy. Alias mismatch is actual: deployment t.test→web-t accepts onlyweb-t upstream, gated registry accepts onlyt.test. Minimal cleanup must retain same grant/room/sidecar/network/holder recovery handle, perform bounded cancellation-safe UNKNOWN full-charge settlement independently of cleanup success, and preserve quarantine/admission until physical closure verified. Do not discard busy/refund/report closed on failure. Separate typed tool-owned selector meaning resolves aliases; no global union or tool-name engine branch. Cancellation/cleanup/settlement-failure tests required before acceptance.
Review probe left C:/Users/qkrgu/AppData/Local/Temp/plan7-readonly-repro-o5z4hgmv. Agent and root verified-path PowerShell cleanup were rejected by automatic approval review (blocked by policy); no further cleanup attempts. User informed; repository unaffected. Keep residual artifact in final limitations.

### Concrete stage04 packets (proposal; independent review before mutation)

TASK-04-N — Tool-owned selector meaning and approved-target binding (B01/B02, source7b3–5). Ownership first core/contracts/tool.py generic ToolTargetBinding metadata; after interface freeze orchestration/engine.py target_aliases, native_binding.py actual caller, capability compiler and gated_manifest.py consumers; gated_strategy.py approved-target resolve. Proposed target_ref_source Literal[mcp_ref,logical_host] defaults mcp_ref and is omitted from default serialization to preserve unchanged descriptor identities. Existing MCP references remain selected; logical_host uses the approved target host independent of deployment aliases, without global union/tool-name branches. Compiler, validator and capability resolver consume the same generic meaning. Capability resolves approved host/port with public core helper, refuses mismatched logical registry name or concrete port before ledger, pins come only from operator registry, approved logical scope plus all literal denials remain. Unknown/ambiguous target fails closed. Acceptance: actual alias reproduction resolved; configured deployment references still work; logical selector rejects deployment ref; pinned target port conflict refused; arguments cannot invent pins; non-gated default serialization/fingerprints unchanged. New metadata meaning/version/resume handled by owner; preserve historical reads. Dependencies03S/P frozen (committed), U shared compiler/app seams handoff required if overlap.

TASK-04-C — Recoverable interrupted opening/settlement (B03, source7b8). Ownership capability/gated_window.py opening/aborted handle and gated_strategy.py execute cleanup seam, control/network_ledger.py/public network-grant port only if current durable recovery cannot expose required reads. Reuse OpenWindow and existing ledger quarantine/settlement; no parallel phase/state store. A reserved or interrupted opening retains SAME grant/window/holder/room/network/sidecar identity, including caller cancellation while reservation commits. Bounded cancellation-safe abort attempts UNKNOWN full-charge settlement independently of Docker cleanup outcome; failed settlement keeps recoverable held handle for idempotent retry. Original failure/cancellation is not replaced by teardown failure. Unverified closure denies both target and local execution; never merely discard busy/refund/report closed. Existing protected job recovery remains; plain interrupted reservations also require durable same-ID reconciliation after restart via Control-owned activity/grant reads and Capability-owned verified closure predicate. No new reservation/quota reset. Acceptance actual Sol repros as regressions, cancel-before/after reservation, foreign cleanup, settlement failure/retry, duplicate abort, process-death plain grant full charge/quarantine, explicit verified-only release, foreign/unknown identity refusal. Closure proof and usage unknown preserved. Dependencies03S/P frozen, no app U source overlap. Exact bounded cleanup/recovery public interface reviewed before mutation.

TASK-04-R — Applicable origin ROE guard (B04, source7b5–7). Ownership capability-owned typed restriction projection and gate resolver/configure_gated; app hash-bound deployment/selection composition after U file handoff. Proposed HttpRestrictedOrigin(host,effective port,scheme) derives from verified operator deployment source/policy hashes; guard all raw transports on that origin host/port before reservation with gated_http_roe_unenforceable. HTTP full still does not authorize arbitrary bytes; unrelated sibling ports remain independently subject to normal scope/selection. No override, guessed parser, raw stdout receipt or model-supplied restriction. Unknown applicable restriction refuses with gated_http_roe_unverifiable, but manual role-name presence alone cannot turn unrelated server-local settings into global authority. Need concrete provider-owned policy/ref applicability contract for manual deployments; no central role/tool-name branch or command-argument parsing. This packet remains design-gated until that interface is proven. Acceptance configured/inherited selected gates refuse restricted selected origins without ledger request, full same, all transports sameport, unrelated permitted endpoint unaffected, local shell remains network-none, changed restriction/resume identity rejected; no target-ready from nft alone.

Physical route decision: no blanket loopback rejection or automatic host.docker.internal replacement. Existing registry may represent an operator-selected actual address; room-local loopback cannot certify host reachability. Add new location/binding metadata only after existing target/registry cannot express required route identity; original target/scope/approval/fingerprint/evidence must remain bound. D1/D6/D7 decisions retained. Physical mock path and actual packet proof remain07, not these injected regressions.

Sol fresh plan review clears04N; root authorizes its typed descriptor interface implementation first with explicit consumer handoff, no04R changes. Sol04C MEDIUM accepted: outstanding plain grants cannot be found through public known-ID lookup or boolean activity query. Explicitly own core/ports/network_grants.py read-only outstanding-grant query plus Control ledger implementation, scoped to bound engagement/trusted run-instance. Capability reconciliation consumes SAME original grants, UNKNOWN idempotent settlement, denial until verified owned physical closure; discovered grants do not authorize transmission/invent holder/resume delegate. Missing physical ownership/holder proof stays unknown. Required absent-sidecar/never-attached recovery case. Trusted reconciliation caller/selection must be concretely frozen before04C mutation.04R remains design-gated; no whole-gate role-name heuristic approved.

TASK-04-N accepted: fresh scoped Sol source review found no concrete issue; bounded calls confirm deployment alias conflict resolved, wrong MCP ref rejected for logical selectors, implicit HTTPS443 accepted/80 denied, default mcp_ref serialization/hash identical. Root Linux five affected files108 tests passed and six-source mypy passed; worker focused170 plus adjacent40, Ruff/diff checks pass. Selector producer/consumer freeze includes core tool metadata, compiler, generic/native binding and gate approved-host/port check. This closes N only; HTTP applicability guard, recoverable cleanup and actual physical route proof remain open.

04C refined public recovery proposal: OutstandingNetworkWindow(grant,state OPEN|QUARANTINED,stored settlement) returned read-only by engagement-bound ledger for exact trusted run/instance, errors never empty. Reuse grant run/instance/epoch opaque_window_tag, existing gate labels/network name and shared workroom key; no caller tags/holder reconstruction/new identity store. Recovery consumes P-restored DockerEndpoint(binary,context,host) and environment fingerprint via existing manager/assembly; manual/unbound or changed endpoint refuses inventory settlement/release. Trusted caller is locked resume path after InstanceDriver.own_run and exact Run binding/metadata, validated activation inventory, before actor driving; transfer_owner=False/live competing host/pending external job cannot mutate grants. Need verify ordering avoids circular readiness blocked by outstanding activity.
Capability proposes ephemeral GatedWindowRecovery retained via existing RuntimeDeps injection, no model tool: reconcile_instance reads original outstanding grants, obtains complete selected-daemon all-state label inventory plus exact-name/label/live-network corroboration, checks no matching sidecar/network and absent or exact owned baseline-none shared room with no unresolved target process/job. Timeout/truncation/error/foreign/duplicate/orphan/conflicting inventory remains UNKNOWN; present sidecar remains UNKNOWN even nft success. Never-created/never-attached grant may prove closure only complete coherent absence/baseline. Same original ID settles UNKNOWN/full charge independently of cleanup; verified physical closure alone permits quarantine ownership release with no refund. In-memory opening/settlement failure retains recoverable handle and denies local/target work; original cancellation/error preserved. No automatic tunnel/address replacement/positive per-pin scope changes.
Independent review must resolve: exact per-instance ownership readiness order vs activity blockade, smallest read-only manager observation API rather than duplicating existing workroom guards, safe absent-resource detection without parsing arbitrary error strings, and shared bootstrap/central_run file handoffs after03U/02C. Packet remains design-gated until fresh Sol confirmation; source mutation not authorized by detailed proposal alone.

04C fresh Sol ordering finding accepted: recover_actor checks outstanding network activity before owner CAS, so requiring outcome.ready before gate reconciliation is circular. Correct owner/seam: InstanceDriver.recover_central_instance under existing exclusive Run/recovery locks first verifies exact original runtime/checkpoint/activation owner-version, original receipts and no pending external jobs; then invokes optional injected structural Capability recovery port for that exact instance and restored P endpoint, BEFORE recover_actor. UNKNOWN full-charge quarantine does not bypass activity/admission and actor transfer remains blocked; complete verified physical closure may release ownership then existing CAS succeeds. central_run keeps original-instance selection, no alternate recovery sequence. Add instance_driver.py explicit ownership; RuntimeDeps structural port and bootstrap injection handoff after U, not a model tool. Manager exposes one read-only recovery observation reusing ownership/policy/live-network guards; ensure_workroom/adoption setup/start prohibited. Required integration: original actor plus outstanding plain grant→trusted closure/read→verified-only release→original CAS, and UNKNOWN counterpart no owner transfer/target/local execution. Absent/neverattached proof stays complete coherent inventory only. Source coding still awaits exact revised interface independent confirmation; new APIs are implementation deliverables, not prerequisites that must already exist before coding.


Fresh Sol04C revised plan/interface CLEAR: root authorizes implementation in staged public interfaces. Trusted inputs original run/instance plus validated activation owner/version, P-restored endpoint/environment injected, no supplied tags/holder/replacementdaemon. Ledger read original grant/state/settlement engagement-scoped failures structured; Capability returns per-window settlement/release outcomes+bounded diagnostics, never actor-ready authority. Manager observation complete/error/owned network-process facts no setup/adoption side effects. Hook after original identity/receipts/jobs quiescence before recover_actor unchanged activity/CAS; UNKNOWN keeps actor blocked and unrelated unfinished execution stays unresolved. Control/core read interface first, capability cleanup/inventory/recovery implementation afterfreeze, runtimeDeps/driver/assemblyconsumer handoffs serial with03U/02C. Unit/injected integration success+UNKNOWN counterpart and same-ID/cancel/failedsettlement/absent-neverattached negatives required before acceptance.04R/physical proof separate.
