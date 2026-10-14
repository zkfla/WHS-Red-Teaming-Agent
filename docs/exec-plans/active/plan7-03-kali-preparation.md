# ExecPlan 03 — Kali 설정·준비·조립 연결

## Status

IMPLEMENTING — 2026-10-14, original Windows checkout, dev at 376b479d. User authorized implementation and stage commits; final review → repair → tests required.

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

## Original numbered requirement coverage

The grouped rows above do not permit partial PASS. At completion, inspect each original numbered implementation requirement in docs/plans/plan7-source-20261014/plan7a.md and attach producer/consumer/test evidence independently.

| Source requirement | Plan requirement | Evidence / disposition |
|---|---|---|
| plan7a implementation 1 | A01 | OPEN |
| plan7a implementation 2 | A01 | OPEN |
| plan7a implementation 3 | A02 | OPEN |
| plan7a implementation 4 | A02 | OPEN |
| plan7a implementation 5 | A02 | OPEN |
| plan7a implementation 6 | A03 | OPEN |
| plan7a implementation 7 | A03 | OPEN |

## Completion Criteria

Every requirement above proven against current state, required reviews addressed, meaningful checks pass, stage commit contains scoped files, preserved user state. Move to completed only then; otherwise retain precise missing requirements and next action. Parent goal remains active until all stages including physical proof and final review/repair/test pass.


### Corrected source evidence and concrete packets (root proposal; review before mutation)

Existing web closed/configured/inherit selection already uses request-local environment_context. central-selection-inputs is enforced indirectly: bootstrap records the hash in component_binding, run records execution_environment_fingerprint, and central_run.bind_run_metadata compares the entire recorded binding/configuration. Do not add a second reader/identity database. prepare_shell_runtime already checks expected_fingerprint through prepared-record restore. resolve_target only performs bounded getaddrinfo and address/scope validation; it does not probe/connect to the target. DNS metadata resolution is distinguished from target traffic.

TASK-03-S (A01,A02; original7a2,3): Freeze capability-owned preparation request and bounded preset interface first. Owner: capability/gated_network_policy.py and dedicated pure validation tests. Reuse existing GatedNetworkPolicy, NetworkBudget, WindowCaps and PinnedRegistry. Add only an explicit operator-selected managed-sidecar preparation request if the existing immutable sidecar_image cannot express the new preparation mode; pinned policy remains inspectable, and missing sidecar without that explicit request stays an error. Pure builder takes validated selected target pins and operator-selected finite budget/caps/preset, with install_capacity0, no mirrors/install proxy. No Docker/image build, DNS, target request or policy mutation in the pure builder. Schema-owned supported presets are the sole defaults consumed by UI/CLI; immutable production image is resolved from the existing source-owned build recipe at execution preparation, never a hardcoded test tag/digest. Interface proposal must name request vs fully resolved policy and prevent unresolved requests from reaching open_window. Dependencies: source review/root approval; no02B core/config/settings edits. Required validation: explicit values, bounds, pins, missing/unpinned sidecar, disabled install route, pure no-side-effect construction. Execution SERIAL_ONLY until public builder/selection interface freeze. Risk: treating an unresolved preparation request as a ready gate.

TASK-03-P (A02,A03; original7a3–7): Ownership capability/shell_preparation.py, shell_runtime.py, shell_image.py only where an existing managed-image recipe can be reused, gated_activation.py/gated_network_policy.py after S freeze, dedicated preparation tests. Extend existing preparation/cache/lock/record flow with selected network policy content hash, immutable resolved sidecar image and selected Docker endpoint identity. An explicit pinned sidecar is inspected locally; an explicitly selected managed sidecar is built from the repository gate-sidecar recipe and converted to its immutable inspected image ID. No implicit pull/replacement for missing pinned images. Use the selected endpoint/context without changing global Docker context/VM. Run bounded nft check in a disposable container (--network none, cap-drop ALL, NET_ADMIN only, no host mounts), feed build_ruleset bytes with LF stdin, no target traffic. Check quota/NFQUEUE requirements on that daemon. Readiness checked_at/display text must not enter immutable identity. Cache/restore verifies both actual images and matching Kali manifest; network policy path relocation with equal content retains identity, content/sidecar/endpoint changes do not. Old recorded Runs use their recorded immutable policy/environment or reject explicitly, never adopt today's managed image/policy. Prepared status and resolved network policy reach the existing runtime/adapter assembly, with catalog failures distinguished from local-only availability. Stable failure codes use the existing safe configuration/availability mechanism (missing image, daemon unavailable, kernel feature unsupported, policy mismatch) and action conditions, not free exception bodies. Dependencies S freeze; no independent central-selection reader. Required validation: fake-runner positive/negative/cache/restore/timeout/cancel; physical limited nft check explicitly separated from fake (root baseline already exit0 on current selected daemon). Execution PARALLEL_AFTER S; ownership handoff gatedpolicy edits serial. Risk: stale ready cache, incomplete resume identity, privileged model process. Model remains cap-drop ALL; NET_ADMIN is disposable preparation sidecar only.

TASK-03-U (A01,A03; original7a1,2,6,7): Ownership app/web/engagement.py, web_policy.py, engagement_catalog.py, existing static/engagement_fields.js/form consumer; app/cli/commands/run.py and bootstrap.py only the actual preparation injection seam after P interface freeze; app/cli/tool_context.py + orchestration/agents/tool_access.py availability projection. Reuse existing form/store/session runner. Add explicit prepare mode and fields for selected target/port, finite preset/limits, execution environment and managed/pinned sidecar choice; show effective values without requiring hand-written JSON. Saving/reading the form cannot build Docker images or call a model. Preparation runs at existing execution startup only. A public validated request-local selection function is shared by web and CLI assembly for the user's selected engagement; explicit closed overrides ambient inherited policy. Do not blindly trust arbitrary metadata, mutate global .env or create another web executor. Preserve HTTP permissions/session references; selected target/port cannot broaden engagement scope. Persist the operator request snapshot and resolved immutable artifact through existing hash-bound artifact paths; validate mismatched content. Projection distinguishes target-ready/local-only and field-specific failure/action conditions. Dependencies S builder freeze and P prepared-result freeze for final wiring; UI pure form validation may proceed earlier with frozen S. Required validation: closed/inherit/pinned/prepared input→store→selected Run assembly, no global-env leakage, view no-build/model, error-code field mapping, recorded policy hash match/mismatch, readiness affects actual gated descriptor/context consumers. Execution PARALLEL_AFTER S with source files disjoint from P; run/bootstrap shared insertion only after explicit interface handoff. Risk: hidden fallback to local-only or ambient policy replacing closed.

Readiness exclusions: packet-level quota/flow enforcement cannot claim HTTP method/path/header/account/session ROE enforcement. The later route/gateway stage must fail closed when those applicable restrictions are not enforceable; this stage must not label that route target-ready merely because nft syntax passes. Target location translation remains stage04, with original logical target/scope preserved. No automatic install route or target request is authorized by a readiness result.
### Sol selected-endpoint finding accepted / S interface refinement

The existing network policy only pins IPs; it does not preserve a form-selected narrower port/protocol. Add a capability-owned immutable endpoint restriction beside registry pins in GatedNetworkPolicy: registry-name → validated selected (port, transport tcp/udp) tuples. New pure builder consumes these explicit endpoint selections, validates keys against selected registry targets and includes the restriction in the artifact/content hash and prepared identity. This narrows original engagement authority; it never adds a destination, allowed port or protocol authority outside scope. Old configured policies without this new selection retain their existing scope-bound behavior and must not be presented as enforcing a narrower preparation selection.

S additionally owns gated_strategy.py solely the endpoint-restriction consumer: reject an invocation port outside an explicitly selected endpoint set before reserving/opening a window, and generate AllowedFlow protocols only from the selected set (current implementation opens both tcp/udp). Keep existing _check_allowed/_check_denials and original EndpointTarget authority intact. This bounded restriction can be wired in S without host/Docker translation or actual target traffic, and avoids storing a decorative unenforced selection. Dedicated tests cover another in-scope port being rejected, unselected UDP rejected for TCP selection, out-of-scope selected port still denied, unchanged old-policy behavior and immutable hash changes. Stage04 later consumes this frozen restriction together with logical→physical route mapping and full applicable ROE enforcement; ownership of gated_strategy.py transfers only after S freeze.

Readiness projection at stage03 means prepared prerequisites, with target route/ROE verification pending; it cannot claim full target-ready from nft alone. The endpoint restriction must already be honored at the invocation boundary in S; unresolved/unsupported selected route requirements refuse execution. Completion/reporting cannot treat prerequisite preparation as target communication or account/session proof.

Root gate: Sol fresh plan review accepted endpoint refinement and found no remaining concrete blocker. TASK-03-S is authorized with Haiku XHigh, disjoint from02-A/B/C; P/U remain gated on public S freeze and explicit later interfaces. This is implementation authorization from the current user goal, not the attached document's imperative text. Source7a all-numbered traces remain OPEN until producer/consumer/preparation proof is complete.

S owner decisions: finite explicit presets are accepted (probe256 deliveries/4MiB/300s; standard4096/256MiB/900s), subject to additional original ROE/Run limits. All pinned targets require explicit endpoint selections for a new preparation builder. Install capacity remains0; positive 1-delivery/1-byte/1s install-window placeholders satisfy the existing WindowCaps shape but cannot obtain an install grant or route. Presets are configuration defaults displayed before the operator chooses them, not execution authorization.

S review checkpoint: Root55 unit tests and Ruff pass; root Linux mypy found18 construction/narrowing issues and assigned typed repairs without ignores. Sol found a MEDIUM actual nested immutability failure: mutating EndpointRestriction.selections changes policy hash and admitted port/protocol, and request/resolved share the mutable object. Require nested read-only mapping, stable serialization and update/delete/external-alias regression before source commit or P/U handoff. No unrelated PinnedRegistry redesign. P must validate a managed request distinctly, then resolve only from a real built/inspected immutable image ID; it cannot pretend to resolve before Docker preparation.
TASK-03-S frozen and accepted: typed repairs and nested read-only endpoint mapping are complete. Fresh Sol recheck found no remaining scoped issue; JSON round-trip, refusal and hash remain stable. Root Linux Python3.10 endpoint tests61 passed and mypy2 source files passed; worker combined new/baseline gated tests201 passed with Ruff. Request-to-resolved registry copy is accepted narrowly to prevent alias leakage, without changing legacy PinnedRegistry semantics. This closes S only; actual preparation/readiness and UI/CLI wiring remain P/U, and no live target proof is claimed.

P freeze checkpoint: root Linux affected preparation/CLI48 tests pass, mypy2 source files and Ruff4 pass. Fresh Sol injected-only reproduction confirms two accepted gaps before commit: explicit pinned Kali plus selected gate records manifest_pathNone, then actual program discovery refuses the fallback repository catalog as shell_image_manifest_mismatch; no wrong-image rows/fake fallback occurs, but prepared identity does not bind its consumed catalog. Managed recorded expected-fingerprint restore unnecessarily calls current recipe hash before _restore and fails if the recipe is absent even when the recorded immutable sidecar remains available. Same Luna owner must bind/verify the actual selected catalog or explicitly refuse missing/mismatch and avoid requiring/rebuilding the current recipe for exact recorded restore. P remains frozen except accepted repairs; U final wiring gated. Physical nft readiness exit0 is prerequisite proof only.

Root approves P repair interface: optional program_manifest_path mirrors existing build_shell_runtime, forwarded through automatic preparation; explicit-image automatic preparation validates supplied or actually consumed default catalog before recording, stores exact cached content/hash, stable missing/mismatch failure, no new global env. Manual mode unchanged; old prepared null-catalog records do not silently adopt current defaults. Exact expected-fingerprint restore defers current managed recipe hashing and uses recorded immutable images. Same Luna owns repairs; fresh independent recheck required.

P repair recheck: Linux57 affected tests, mypy2 and Ruff4 pass; Sol confirms both original fixes, but reproduces one added API regression: managed default selection with supplied catalog mutates content yet active cache returns old fingerprint/catalog. Root approves smallest intent-preserving correction: optional program_manifest_path is explicit-image automatic-preparation only; reject it without explicit shell policy before preparation/cache. Default managed generator stays unchanged, paths remain outside identity. Expected-fingerprint explicit supplied content must not silently replace/ignore selected semantics; exact recorded catalog remains authority with mismatch refusal. Same-owner repair and fresh check still required before P commit.

TASK-03-P accepted: final scoped Sol recheck found no remaining concrete issue; exact restore matches supplied catalog or rejects changed content, no override uses recorded catalog, default-managed override rejected before environment/cache, recipe-free exact restore retained. Root Linux59 affected tests and mypy2 pass; Ruff4 pass. Worker broad184 passed before accepted repairs, latest59 after repair; physical nft exit0 was prerequisite-only. Scoped source commit closes P, not full03 U/route/target readiness. Prepared result freeze transfers public parameters/status/resolved policy/hash/endpoint/timestamp to U wiring; only network_prerequisites_ready may be displayed pending04.

### U exact field/startup packet (proposal; independent review before mutation)

Extend existing WebEngagementDraft/WebPolicySelection and form fields, not a second UI/runtime. kali_network_mode adds prepare; UI default closed retained. Prepare endpoint choices use origin hostname plus strict port/tcp|udp, canonical duplicate rejection and S-owned per-host endpoint bounds; all selected origin hosts covered. Reuse SelectedEndpoint/GatedLimits/preset catalog for persisted kali_endpoints, exactly one kali_limits_preset/kali_limits, selected managed/pinned sidecar mode/image and optional Docker context. Existing configured path/hash unchanged. New optional persisted fields omit absent defaults so legacy YAML serialization/hashes remain. Form flat custom limit fields map to S-owned limits, no copied numeric allowlist. Nonprepare modes reject prepare-only fields; prepare requires existing automatic web-policy selection and rejects configured policy path. Save/parse performs no DNS, Docker or model calls. Scope checks consume original core host/port meaning; endpoint selection never grants authority.

One public request-local startup selection shared by CLI run.build and actual web host/session assembly: returns selected policy artifact path/hash and environment overrides, never mutates global .env. Closed explicitly clears ambient gate path; inherit retains existing ambient selection; configured validates current hash-bound policy; prepare resolves bounded IPv4 DNS metadata at startup, checks original scope/denials, calls S pure builder and atomically persists content-addressed request artifact through existing engagement .mcp artifact ownership/mode. Fresh Run may resolve pins; resumed Run uses recorded immutable artifact/pins, verifies current selection/endpoint/environment and rejects DNS/content drift without rewriting that artifact or adopting today defaults. Docker context is request-local, conflicting explicitly pinned shell context refuses. Existing automatic default managed Kali preparation works when shell policy path is absent; no new shell-policy-required blocker. Auto-setup disabled with selected preparation returns existing P coded refusal.

P wiring: pass selected network_policy_path to prepare_shell_runtime; consume resolved ShellRuntime.network_policy only after network_prerequisites_ready, with its selected-policy hash distinct from resolved policy content hash for managed sidecars. Never compare selected request hash with resolved image-bearing policy as if identical; both identities already P-bound. Closed cannot fall back to ambient gate. Preserve no-selection legacy behavior, existing selection-input and execution-environment fingerprints. Trace web host.prepare/session_runtime actual path before declaring shared consumer coverage; compiler/app ownership handoff avoids concurrent writes with04N.

Projection: local_only versus network_prerequisites_ready with route/ROE pending, never target-ready from nft. Field errors use safe codes/action conditions: Kali image failure points to Kali/execution environment, sidecar missing points to pinned/managed sidecar, daemon failure to context, policy mismatch to exact resume selection. No raw error-body logs. UDP remains operator-selectable S protocol, but later04 applicable HTTP-origin guard covers all raw transports on that selected origin port.

Acceptance: pure form/selection bounds/default/legacy-byte-hash tests; fake DNS/runner startup closed over ambient, managed/pinned prepare, context conflict, scope/port denial, policy/DNS/endpoint drift resume refusal, no-selection legacy; actual web+CLI preparation consumer wiring and stored resolved identity; views/saves no side effects; error fields correctly distinguish Kali/sidecar/catalog. Linux type/lint and JS syntax as applicable; fakes labeled, no target traffic/live model claim. Root accepted six routine selection directions with these corrections; new schema packet awaits fresh Sol plan review before mutation.

Fresh Sol03U exact packet plan review found no concrete blocker. Root authorizes same Haiku03U implementation under corrected packet, actual web/CLI consumer tests and shared-file handoff required. This is source implementation authorization, not runtime readiness/whole03 completion. No model/DNS/Docker on save; recorded immutable resume artifact remains authority.

U concrete resume consumer gap accepted: resume._run loads config before shared run.build outside selected startup environment, so generated web roles absent ambient settings are rejected before injection. Root extends ownership narrowly to app/cli/commands/resume.py: select existing request-local engagement startup, config load under existing environment_context(startup.environment), forward engagement_path at both resume build sites. Add actual selected-role config/resume seam and no-global-leak regression; preserve recorded pins/identity/closed/inherit, no unrelated resume refactor or Ccentral_run overlap.
