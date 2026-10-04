> 이 문서는 작성 당시의 감사·검토·작업 기록이다. 중간 상태와 검증 수준을 그대로 보존한다. 최신 구현·CI·병합 상태는 [목차](../README.md)의 최종 계획을 기준으로 읽는다. 인용 행 번호는 검토 당시 기준이며 소스 링크에 현재 행 번호를 추정해 붙이지 않았다.

# impl-a-capability-report (worker A: capability)

## 0. 긴급 API 계약 (루트 직독용, 2026-10-04)

상태: registration.py SyntaxError 수정됨. 소유 변경 14개 파일 python -m py_compile EXIT=0.
원칙: orchestration/ 은 redteam.capability 를 import 하지 않는다(M30). 중앙 소비는 optional table 인자 또는 RuntimeDeps 주입으로만 받는다. 중앙이 capability 전역 dict 를 읽는 형태 없음.
kind 정리: mcp_profile 단일 bundle kind 로 접음. contribution_id = 프로필명(7종). factory(context: CapabilityFactoryContext) -> McpProfileBundle. requires=() (4슬롯 강제 없음). 구 4kind 상수/슬롯 삭제.

### 0.1 내장 Source declaration

- extension_id: capability-builtin, version: 1
- kind: mcp_profile (유일). contribution_id: vdb_readonly | vdb_readonly_extended | page_text_readonly | execution | web_search_tavily | workspace_read | workspace_write
- factory 시그니처: factory(context: CapabilityFactoryContext) -> McpProfileBundle. context.env: Mapping[str,str] (스냅샷), context.extra: Mapping[str,Any]. 내장 팩토리는 context 무시(고정 데이터). requires=() .
- McpProfileBundle dataclass(frozen): profile: str, apply_settings: ProfileContributor|None, build_adapter: AdapterContributor|None, bind_discovery: DiscoveryBinder|None, default_target_types: tuple[str,...]. 전부 선택 슬롯. 설정워커/루트는 조립 시 bundle 테이블을 통째로 교체 가능.
- 나머지 내장 기여(동일 선언에 포함): kind capability.action_binding (id operation:web_search 등), kind capability.retry_policy (id default), kind capability.execution_recipe (id recipe:line_count), kind capability.server_role (id web-http 등). B/루트 kind 와 무충돌.

### 0.2 제공자 공개 함수 (시그니처 exact, 필수/선택 표기)

capability/settings.py (소비자: app/cli _shared·interactive·tools, capability/bootstrap):
- read_server_environment(prefix: str, *, source: Mapping[str,str]|None = None) -> dict[str,str] [필수 Hoist 대상 아님, 교체 가능점] : prefixed base 는 redteam.settings.read_prefixed_values + env_snapshot 로 읽고, 비밀간접·passthrough·공유키 extras 만 추가. 설정워커 공용 loader 가 extras 유지 조건으로 교체 가능.
- MCPServerSettings.from_environment(server_id: str, *, env: Mapping[str,str]|None = None) -> MCPServerSettings [필수 유지]
 - MCPServerSettings.from_environment(server_id: str, *, env: Mapping[str,str]|None = None, bundles: Mapping[str,McpProfileBundle]|None = None) -> MCPServerSettings [필수 유지, bundles 는 조립 선택 테이블]
- mcp_server_ids_from_environment(env_var: str = REDTEAM_MCP_SERVERS, *, source = None) -> list[str] [호환 유지] : 내부는 redteam.settings.mcp_server_list(source, env_var) 위임.

capability/registration.py (소비자: capability/settings·bootstrap, 루트 조립):
- profile_bundle(profile: str) -> McpProfileBundle|None [선택 주입 전 기본값]
- register_profile_bundle(bundle: McpProfileBundle) -> None [루트 조립용, 중복 등록은 ValueError]
 - register_profile_bundle(bundle: McpProfileBundle, *, replace: bool = False) -> None [루트 조립용, 중복은 ValueError, replace=True 교체]
 - build_mcp_tool_gateway(server_ids, *, health_*, profile_bundles = None), open_mcp_tool_gateway 동일, _build_gateway 내부 fresh dict 복사 후 from_environment·_create_adapter·apply_profile_discovery_binding 까지 동일 객체 전달. _create_adapter(settings, transport, bundles = None) [호환 유지].
- create_adapter_for_settings(settings: Any, transport: Any, bundles: Mapping[str,McpProfileBundle]|None = None) -> Any [필수, bootstrap _create_adapter 가 호출]
- apply_profile_settings(context: ProfileSettingsContext, bundles = None) -> None [필수, settings.from_environment 가 호출]
- apply_profile_discovery_binding(descriptor: Any, settings: Any, bundles = None) -> str|None [필수, bootstrap 발견 검증이 호출]
- default_target_types_for_kind(kind: Any) -> list[str] [필수]
- select_server_target_type(server_id: str, tool_id: str) -> str|None [선택]
- validate_server_configured_discovery(server_id, descriptors, server_bindings, server_schemas) -> None [필수]
- apply_server_catalog_target_types(server_id, server_bindings, target_types: dict) -> None [필수]

capability/action_binding.py (소비자: compiler·execute·target_binding — 전부 주입형):
- get_operation_binding(operation: str, bindings: Mapping[str,OperationTargetBinding]|None = None) -> OperationTargetBinding [필수; None 이면 내장 테이블]
- should_bind_target_arguments(operation, bindings = None) -> bool [필수]
- should_bind_known_path(operation, bindings = None) -> bool [필수]
- requires_url_target(operation, bindings = None) -> bool [필수]
- requires_exact_url_match(operation, bindings = None) -> bool [필수]
- is_retry_safe_action(capability: str, operation: str, arguments: Mapping|None = None, *, policy: RetryPolicy|None = None) -> bool [필수; None 이면 내장 정책]
- declares_retry_safe(namespace: str, operations: Collection[str], *, policy = None) -> bool [필수; 기술자 retry_safe 선언용, 빈 바인딩은 False]
- RetryPredicate = Callable[[Mapping[str,Any]], bool]. RetryPolicy 필드: safe_actions, http_safe_methods, http_mutating_argument_keys, conditional_predicates: tuple[tuple[str,RetryPredicate],...] (= (web.http_request, 내장 GET/HEAD·무바디 판정)).
 - OperationTargetBinding·RetryPredicate·RetryPolicy·ExecutionRecipeSpec = core.provider_contracts 실별칭 (capability 재정의 없음, core 만 실체). RetryPolicy.permits(capability, operation, arguments) -> bool [중앙 execute 가 직접 사용]. RetryPolicy 필드: safe_actions, http_safe_methods, http_mutating_argument_keys, conditional_predicates (= (web.http_request, 내장 GET/HEAD·무바디 판정)).
- 전역 predicate 레지스트리 없음(삭제). 조건 판정은 정책이携行. requires 참조 없음(함수 주입).
 - 전역 predicate 레지스트리 없음(삭제). 조건 판정은 정책 객체가 함께 운반. requires 참조 없음(함수 주입).

capability/execution_recipes.py (소비자: engine — 주입형):
- builtin_execution_recipes() -> dict[str,ExecutionRecipeSpec] [선택]
- get_execution_recipe(name: Any, *, recipes = None) -> ExecutionRecipeSpec|None [필수]
- get_execution_recipe_for_operation(operation: str, *, recipes = None) -> ExecutionRecipeSpec|None [필수]
- default_execution_recipe(*, recipes = None) -> ExecutionRecipeSpec [필수]
- execution_tool_operations() -> frozenset[str] [필수]
- is_execution_operation(operation: str, *, recipes = None) -> bool [필수, engine 2814 조건용]
- ExecutionRecipeSpec.build_arguments(*, asset_ref, path, engagement_id, run_id) -> dict [필수]
- execution_bridge 공개 함수(execution_agent_options, bind_execution_arguments, agent_tool_visible)는 동일 이름으로 *, recipes: Mapping|None = None 추가. 호출 없을 때 내장 테이블.

capability/server_roles.py (소비자: capability/deployment·servers/__main__, app/mcp_deployment 패치노트):
- launcher_role_names() -> tuple[str,...] [필수]
- deployment_role_names() -> tuple[str,...] [선택]
- role_spec(role: str) -> ServerRoleSpec|None [필수]
- role_operations(role: str) -> dict[str,list[str]] [필수, deployment ROLE_OPERATIONS 대체]

capability/catalog.py (소비자: engine·nodes, writing_schema caller):
- MCPToolCatalog.executable_pairs() -> list[tuple[str,str]] [신규 필수, 정식 (capability, operation) 집합. caller 가 registry/catalog pairs 전달 방향]
- resolve(tool_id) -> ToolDescriptor|None [필수, 정식ID 우선·별칭 단일대응·충돌 거절]
- list_tools_for_action / list_candidate_metadata / supports / finalized [기존 유지]

naming (capability/adapters/mcp/naming.py):
- escape_tool_segment(value: str) -> str [원문 exact 보존, quote+safe + 마침표 %2E]
- unescape_tool_segment(value: str) -> str [역변환, 리터럴 마침표는 ValueError]
- build_canonical_mcp_tool_id(capability, server_id, remote_tool_name) -> ToolId [정식 3구간]
- parse_canonical_mcp_tool_id(tool_id) -> tuple[str,str,str] [가역 분해]
- is_canonical_mcp_tool_id(value) -> bool / legacy_alias_for_tool_id(value) -> str|None [호환 유지]

### 0.3 루트 적용용 패치 API (call-site exact)

R1 compiler.py (루트 소유, 조건 2건):
- 377: if operation == web_search: target_values = {} → if not should_bind_target_arguments(operation, bindings): (bindings 는 RuntimeDeps 신규 필드 capability_bindings 또는 컴파일러 인자. should_* 5종은 R5 core 이전 후 redteam.core.provider_contracts 에서 import, capability import 금지).
- 384-388: if operation == endpoint_discovery and ... → if should_bind_known_path(operation, bindings) and ... (이하 동일).

R2 nodes/execute.py (루트 소유, 230-238): LOW·retryable·max_retries·게이트 유지, 판정만 교체:
 - and descriptor.retry_safe → and (descriptor.retry_safe or deps.신규필드retry_policy.permits(proposal.capability, proposal.operation, 제안 arguments)). permits 는 core RetryPolicy 메서드(정적 집합+조건 predicate 함께 판정, policy None 일 때 거부이므로 조립부가 builtin 기여로 주입). op==http 리터럴 금지. capability import 없음.

R3 engine.py (루트 소유): 2814 if step.operation in EXECUTION_OPERATIONS → if is_execution_operation(step.operation, recipes=deps.신규필드execution_recipes). 566/1694/2821 bind_execution_arguments·execution_agent_options 호출에 recipes=deps.신규필드 전달(선택 인자라 미전달도 동작).

R4 RuntimeDeps (루트 소유 app/runtime_components.py): 신규 선택 필드 3건 — capability_bindings: Mapping|None=None, retry_policy: RetryPolicy|None=None, execution_recipes: Mapping|None=None. 조립부가 mcp_profile·retry_policy·execution_recipe 기여에서 해석해 주입. 없으면 각 소비자는 내장 기본으로 동작(거부 아님, 재시도 판정 None 정책은 부정).

R5 core 약속모듈 초안 (루트가 core 에 추가, (a) 경로): 모듈명 제안 redteam/core/provider_contracts.py — OperationTargetBinding(5필드), RetryPolicy(4필드)+RetryPredicate 별칭, ExecutionRecipeSpec(7필드)+build_arguments. 초안 전문은 본 보고서 4절. 추가 전에는 orchestration 수정분 적용 금지.

R6 app/mcp_deployment.py (루트 소유): server_roles.role_spec/role_operations 로 소스 전환 가능(동작 동일). reporting.py 는 별도 워커 담당 — 본 보고서는 제공 자료 노출 수준만 언급.

--- (0절 끝, 이하 구현 상세는 작성 중) ---

## Final contract (actual code, supersedes section 0 where different)

Status: py_compile EXIT=0 on all owned files (powershell reports EXIT=True for success).
No pytest/ruff/mypy run. No commit/push. No new deps.

### A. Amendments to section 0

- settings env source is now redteam.core.environment directly (env_snapshot, read_prefixed_values, mcp_server_list). The earlier redteam.settings hop is gone; behavior identical since settings.py is a thin re-export of core.environment.
- core/provider_contracts.py is REAL (root added): OperationTargetBinding(5 fields), RetryPolicy(safe_actions, http_safe_methods, http_mutating_argument_keys, conditional_predicates) + permits(capability, operation, arguments), ExecutionRecipeSpec(7 fields) + build_arguments(asset_ref, path, engagement_id, run_id). capability/action_binding.py and capability/execution_recipes.py hold NO local dataclass and NO predicate registry; they alias-import these names from core and keep only builtin rows + thin provider-default functions.
- RuntimeComponents (app/runtime_components.py, root-owned) REALLY has operation_bindings / execution_recipes / retry_policy (all default None). Root assembly injects; None means builtin defaults.
- core/contracts/tool.py REALLY has ToolDescriptor.retry_safe: bool = False. capability/bootstrap.py sets it per descriptor via declares_retry_safe; nodes/execute.py already reads descriptor.retry_safe (root-owned).

### B. Public provider APIs, exact signatures (required vs optional)

Tables below: [R]=required consumer call, [O]=optional override (None = builtin default).

registration.py (consumers: capability/settings.py, capability/bootstrap.py, root assembly):
- profile_bundle(profile: str) -> McpProfileBundle | None [O, default lookup]
- builtin_profile_bundles() -> dict[str, McpProfileBundle] [R for root: fresh per-run map source]
- register_profile_bundle(bundle: McpProfileBundle, *, replace: bool = False) -> None [legacy compat only; per-run path uses injected maps, not this global]
- apply_profile_settings(context: ProfileSettingsContext, bundles: Mapping[str, McpProfileBundle] | None = None) -> None [R]
- create_adapter_for_settings(settings: Any, transport: Any, bundles: Mapping[str, McpProfileBundle] | None = None) -> Any [R]
- apply_profile_discovery_binding(descriptor: Any, settings: Any, bundles: Mapping[str, McpProfileBundle] | None = None) -> str | None [R]
- default_target_types_for_kind(kind: Any) -> list[str] [R]
- select_server_target_type(server_id: str, tool_id: str) -> str | None [O]
- validate_server_configured_discovery(server_id, descriptors, server_bindings, server_schemas) -> None [R]
- apply_server_catalog_target_types(server_id, server_bindings, target_types: dict) -> None [R]
- KIND_MCP_PROFILE = mcp_profile (single bundle kind). contribution_id = profile name (7 builtins). factory(context: CapabilityFactoryContext) -> McpProfileBundle. requires = (). No mcp_adapter / mcp_discovery_validator / mcp_target_types kinds (deleted).
- McpProfileBundle frozen: profile: str, apply_settings: ProfileContributor | None, build_adapter: AdapterContributor | None, bind_discovery: DiscoveryBinder | None, default_target_types: tuple[str, ...].

settings.py (consumers: app/cli/_shared.py, capability/bootstrap.py):
- read_server_environment(prefix: str, *, source: Mapping[str, str] | None = None) -> dict[str, str] [R, replaceable by settings owner if extras kept]
- MCPServerSettings.from_environment(server_id: str, *, env: Mapping[str, str] | None = None, bundles: Mapping[str, McpProfileBundle] | None = None) -> MCPServerSettings [R; env accepts common snapshot, bundles is per-run table]
- mcp_server_ids_from_environment(env_var: str = REDTEAM_MCP_SERVERS, *, source: Mapping[str, str] | None = None) -> list[str] [compat passthrough to core mcp_server_list]

action_binding.py (consumers: compiler/execute/target_binding via injection, all optional tables):
- get_operation_binding(operation: str, bindings: Mapping[str, OperationTargetBinding] | None = None) -> OperationTargetBinding [R]
- should_bind_target_arguments(operation, bindings=None) -> bool [R]
- should_bind_known_path(operation, bindings=None) -> bool [R]
- requires_url_target(operation, bindings=None) -> bool [R]
- requires_exact_url_match(operation, bindings=None) -> bool [R]
- is_retry_safe_action(capability: str, operation: str, arguments: Mapping | None = None, *, policy: RetryPolicy | None = None) -> bool [R; thin: active.permits(...) OR web.http_request GET/HEAD-no-body fallback so custom policies without a stored predicate still work]
- declares_retry_safe(namespace: str, operations: Collection[str], *, policy: RetryPolicy | None = None) -> bool [R for bootstrap descriptor flag; empty ops = False]
- BUILTIN_OPERATION_BINDINGS: tuple (default / web_search(no-target-bind) / endpoint_discovery(known-path) / page_read(url+exact)). BUILTIN_RETRY_POLICY: safe_actions={recon.service_discovery, recon.endpoint_discovery, analysis.response_analysis, mobile.static_analysis}, http_safe_methods={GET, HEAD}, mutating_keys={body, data, json, payload}, conditional=((web.http_request, builtin GET/HEAD-no-body predicate),). No global predicate registry.
- kinds: capability.action_binding (id operation:<op>), capability.retry_policy (id default). No requires refs (plain injection).

execution_recipes.py (consumer: engine + execution_bridge via injection):
- builtin_execution_recipes() -> dict[str, ExecutionRecipeSpec] [O]
- get_execution_recipe(name: Any, *, recipes=None) -> ExecutionRecipeSpec | None [R]
- get_execution_recipe_for_operation(operation: str, *, recipes=None) -> ExecutionRecipeSpec | None [R]
- default_execution_recipe(*, recipes=None) -> ExecutionRecipeSpec [R, line_count]
- execution_tool_operations() -> frozenset[str] [R, 4 command_* ops]
- is_execution_operation(operation: str, *, recipes=None) -> bool [R]
- ExecutionRecipeSpec.build_arguments(*, asset_ref, path, engagement_id, run_id) -> dict [R]
- kind: capability.execution_recipe (id recipe:<name>). No requires refs.

server_roles.py (consumers: capability/deployment.py, servers/__main__.py; app/mcp_deployment.py via patch note):
- launcher_role_names() -> tuple[str, ...] [R]
- deployment_role_names() -> tuple[str, ...] [O]
- role_spec(role: str) -> ServerRoleSpec | None [R]
- role_operations(role: str) -> dict[str, list[str]] [R]
- kind: capability.server_role (id = role).

naming.py (consumers: registry/catalog/manifest/bootstrap):
- escape_tool_segment(value: str) -> str [exact-preserving percent-escape; quote(value, safe=) + dot->%2E; no strip/lower]
- unescape_tool_segment(value: str) -> str [inverse; literal dot raises]
- build_canonical_mcp_tool_id(capability, server_id, remote) -> ToolId [capability normalized only]
- parse_canonical_mcp_tool_id(tool_id) -> (capability, server_id, remote) [reversible]
- is_canonical_mcp_tool_id(value) -> bool; legacy_alias_for_tool_id(value) -> str | None [compat]
- build_mcp_tool_id(namespace, remote) -> ToolId [legacy 2-part, compat only]

catalog.py: MCPToolCatalog.resolve(tool_id) [canonical-first; legacy 2-part alias only on unique catalog match; collisions refused with reason; no guessing]; executable_pairs() -> list[tuple[str, str]] [NEW, authoritative (capability, operation) set for catalog binding]; supports/finalized/list_tools_for_action/list_candidate_metadata unchanged.

### C. Owned orchestration consumers (done, no capability import)

- target_binding.validate_network_arguments(target, arguments, *, operation="", bindings: Mapping[str, OperationTargetBinding] | None = None) -> None. bindings=None keeps legacy page_read-strict behavior. Root wires RuntimeComponents.operation_bindings here. No redteam.capability import.
- execution_bridge: agent_tool_visible(..., *, execution_ready, recipes=None), execution_agent_options(..., *, recipes=None), bind_execution_arguments(..., *, recipes=None), plus internal _arguments_for_facts/_configured_arguments(..., recipes=None). recipes=None keeps legacy line_count/wc -l/10s behavior. Root wires RuntimeComponents.execution_recipes here. No redteam.capability import.
- is_retry_safe_action(..., *, policy=None): root wires RuntimeComponents.retry_policy here (execute node calls policy.permits directly; our function is the provider-side default).

### D. Root patches (shared files only, exact call-sites)

P1 candidates/compiler.py (root-owned): web_search branch -> if not should_bind_target_arguments(operation, bindings_from_deps): ; endpoint_discovery branch -> if should_bind_known_path(operation, bindings_from_deps) and ... . Import the five predicates from redteam.core.provider_contracts path after root re-export (or from capability.action_binding only inside root-owned assembly, never in orchestration central modules). bindings come from RuntimeComponents.operation_bindings.

P2 nodes/execute.py (root-owned, keep LOW + retryable-error + max_retries + gates): replace fixed known-passive op list with (descriptor.retry_safe or retry_policy.permits(proposal.capability, proposal.operation, proposed_arguments)) where retry_policy = RuntimeComponents.retry_policy (None = deny). No op==http literal. No capability import.

P3 engine.py (root-owned): execution-visibility/options/bind call-sites pass recipes=RuntimeComponents.execution_recipes; operation-membership check uses is_execution_operation(step.operation, recipes=...). Optional args so missing injection still runs builtin.

P4 app/bootstrap.py + app/mcp_deployment.py (root-owned): build fresh bundle map per run = {**builtin_profile_bundles(), **selected extension mcp_profile contributions}; duplicate ids = explicit error; pass as profile_bundles into capability build/open/_build_gateway and from_environment/_create_adapter/discovery-bind chain. role source switch: import role_spec/role_operations from capability.server_roles (same values).

P5 reporting.py: NOT touched (separate worker owns M25 metadata-first). Only exposure: deployment public_metadata target/session keywords stay available for that worker; no API needed from this side.

### E. Existing registry API notes (unchanged public surface)

- MCPToolRegistry.register/replace_server_tools(server_id, descriptors): overwrite-per-server, no merge; duplicates inside one server rejected.
- MCPToolCatalog lookups + resolve (see B) + executable_pairs (new).
- MCPServerSettings.from_environment (see B).
- build_mcp_tool_gateway / open_mcp_tool_gateway(server_ids, *, health_*, profile_bundles=None): per-run fresh dict copy; empty list = in-process example slice.

### F. Changed files (owned only)

- capability/registration.py: single mcp_profile bundle kind + 7 bundles + contribution declarations.
- capability/settings.py: env via core.environment; from_environment bundles param.
- capability/bootstrap.py: profile_bundles plumbing; retry_safe + target-type + discovery via registration.
- capability/action_binding.py: core aliases + builtin binding rows + builtin retry policy + thin predicates.
- capability/execution_recipes.py: core alias + builtin line_count recipe + injection readers.
- capability/server_roles.py (new): launcher/deployment role tables + kinds.
- capability/deployment.py: ROLE_OPERATIONS + connection_environment from role_spec (provision deny-gate + root-moved _declared_host_seeds/deployment_metadata/check_deployment_scope preserved).
- capability/servers/__main__.py: choices from launcher_role_names.
- capability/adapters/mcp/naming.py: reversible percent-escape + canonical 3-part builders.
- capability/catalog.py: canonical-first resolve + executable_pairs.
- capability/manifest.py: canonical 3-part accept + namespace validation.
- capability/registry.py: canonical-first resolve path.
- capability/adapters/mcp/example.py: retry_safe via declares_retry_safe.
- orchestration/target_binding.py: optional bindings injection, core import only.
- orchestration/execution_bridge.py: optional recipes injection, core import only.

### G. Behavior preservation evidence

- Env names, error strings, exact-match strength, reject-on-conflict/no-overwrite, default-deny, approval/scope/schema checks, evidence linkage, custom-profile restore, LLM-led flow: all kept; provider moves are data relocations with identical predicates.
- Old 2-part IDs: never rewritten in stored artifacts; alias resolution only from stored binding or unique catalog match; collisions explicitly refused.
- Execution bridge None-path returns byte-identical legacy dict (wc -l, 10s).
- Target binding None-path keeps page_read-strict flags.
- provision() still refuses on metadata conflict and builds the private dir with O_EXCL; check_deployment_scope deny gate intact.

### H. Unfinished / unverified / race risks

- Shared worktree: root/B/config workers edit core/app/engine/nodes/reporting concurrently; this report pins behavior as of this turn. Re-read root skeleton files before final assembly (extensions.py, app/extensions.py, planning.py changed under us once already).
- pytest/ruff/mypy NOT run (forbidden); only py_compile EXIT=0.
- reporting.py M25 metadata-first is the other worker; our P5 is exposure-only by design.
- streamable_http adapter retry_safe: bootstrap overwrites per descriptor at discovery, so adapter-level default is non-authoritative; left as-is.
- writing_schema caller-passes-pairs direction (R07/catalog membership migration step 5 in earlier instructions): catalog now exposes executable_pairs; the actual caller rewiring in core/action_semantics + registry/catalog membership checks is root-owned and NOT done here (owned-file boundary).

### I. Impact map (actual code basis)

- M06 (action/target binding data): DONE via BUILTIN_OPERATION_BINDINGS + optional injection.
- M08 (execution recipes): DONE via BUILTIN_EXECUTION_RECIPES + optional injection.
- M14/R04 (canonical identity + single resolver): DONE via naming.py + catalog/registry resolve + manifest accept.
- M25 (launcher/deployment roles): DONE provider-side via server_roles.py; app/reporting consumption is root/other-worker patch notes.
- R07 (env boundary): DONE via read_server_environment + core.environment direct.
- M30 (team boundary): KEPT - orchestration/* import core only, zero redteam.capability imports.
- M09 (deployment pure-move): PRESERVED - _declared_host_seeds/deployment_metadata/check_deployment_scope untouched.

---

## 정정 푸터 — 오케스트레이터 (2026-10-04, 루트 통보·실물 확인)

- 본 보고서 §I의 M14/R04 "DONE" 주장은 과대였다(재개정). 당시 어댑터 탐색 경로가 정식 3구간 식별자를 실제 생성하지 않고 해석도 정식 경로로 수렴하지 않아 M14는 불완전 상태였다.
- 루트가 직접 보완 완료(실물 확인): (1) 레지스트리 해석의 정식 경로가 물리 3요소 정확 일치만 채택, 없으면 None(잘못된 서버 대체 제거), 중복 거절. (2) 일반 스트리밍 HTTP 어댑터 탐색이 정식 식별자 실제 생성(streamable_http.py:55), 고정 식별자 명시 어댑터는 기존 정확 검사 유지. (3) 해석 등록부가 카탈로그 단일 해석으로 기존 별칭을 정식 식별자에 결속(모호 시 건너뜀), 앱 조립 2곳 호출. (4) P1~P4 소비 완료.
- A 구현분(식별자 빌더/파서, 카탈로그 정식 우선 해석·실행 가능 쌍 목록, 수용 검증)은 그대로 유지된다.
