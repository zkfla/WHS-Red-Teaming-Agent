# Research A - DIRECT selected-tool execution contract (read-only)

Status: investigation only. No product edits, no tests, no commits.
Base: working tree around docs/agent/validation-contract-20261005/root-investigation.md and research-orchestrator-early.md. Uncommitted CLI changes untouched.
Naming note: exact contract and field names below are intentional per explicit request, overriding the repo role-naming style for this report only.

## 1. Actual failure (one sentence)

A registered tool whose input schema is valid but which has no mandatory action-operation binding cannot be proposed, executed, or resumed today: catalog build fails or the tool is hidden, gateway/compiler/execute reject it, and approval/resume never durably bind the chosen tool because the approval contract has no tool field.

## 2. Evidence lines

- Approval contract lacks chosen-tool field: src/redteam/core/contracts/action.py:21 class ActionProposal has capability (:27), operation (:28), target/arguments/reason, no tool_id. Tool travels only transiently in src/redteam/core/contracts/policy.py ControlContext.selected_tool_id.
- Invocation copies legacy operation: src/redteam/core/contracts/tool.py:69-76 ToolInvocation.operation is required, comment says copied from ActionProposal.operation; src/redteam/core/convert.py:42-63 build_tool_invocation(proposal, decision, tool_id) sets operation from proposal.operation. The tool comes from a caller arg (execute passes step.tool_id), not from the approval.
- Mandatory binding at build: src/redteam/capability/catalog.py:128 raises when operation bindings miss an explicit mapping for a registered tool; supports(tool_id, operation) at :180; gateway enforces at src/redteam/capability/gateway.py:233-236 with operation_not_allowed_for_tool; unbound discovery is explicitly non-executable per src/redteam/capability/bootstrap.py:166.
- Settings and roles harden the same coupling: MCPServerSettings tool_operations must exactly match allowed_tools and pass canonical_action_key (src/redteam/capability/settings.py); ServerRoleSpec.operations is the role tool-behavior table (src/redteam/capability/server_roles.py).
- Compiler re-searches by behavior: orchestration/candidates/compiler.py _resolve_tool_with_reason requires tool_matches_action_namespace plus catalog.list_tools_for_action(capability, operation) plus capability_tags/operations filter; mismatch yields tool_outside_capability_namespace, no_compatible_tool, or no_tool_for_operation. orchestration/candidates/catalog.py bound_operations additionally requires catalog.get(tool) equality plus list_tools_for_action membership.
- Execution re-requires behavior equality: src/redteam/orchestration/nodes/execute.py:231-242 binding_ready needs step.capability equal proposal.capability and step.operation equal proposal.operation plus namespace match plus descriptor.capability_tags plus healthy; :295-296 and :387 fail closed to tool_binding_invalid.
- Admission re-requires binding: engine validate_current_action (src/redteam/orchestration/engine.py:2824 proposal-differs check has no tool comparison; :2841 requires step.operation in bound_operations(catalog, tool, step.capability) else no_tool_for_operation). Context exposure likewise filters to non-empty bound_operations: engine.py:579 writing schema, :673 execution options, :704 tools dict; compile_actions no_tool_for_operation at :1918/:1989/:2024.
- Resume binds behavior, not tool: state pending_action_id and pending_approval_id (src/redteam/orchestration/state.py:100; resume_graph_state :194-226 takes action_proposal plus plan_step); policy resume branch matches pending_action_id equal proposal.action_id (src/redteam/orchestration/nodes/policy.py:130) and forwards selected_tool_id from transient plan_step (:166). Runner refuses EXECUTE/WORKER replay and re-links via register_pending_proposal (src/redteam/orchestration/runner.py _UNSAFE_REPLAY_NODES, recover_runtime/resume_recovered, _register_pending_checkpoint). Audit redaction cannot reconstruct the tool.
- Target semantics are behavior-keyed: core/provider_contracts.py OperationTargetBinding table (default/web_search/endpoint_discovery/page_read) looked up by operation string; engine compile/admission pass operation into validate_network_arguments, bind_asset_arguments, validate_argument_target; ToolDescriptor.target_types plus argument_bindings exist (tool.py:30-35) but are checked only after the behavior gate.
## 3. Minimal compatible DIRECT contract (per Sol steer)

Goal: registered tool_id plus target plus normalized arguments plus reason suffice; no synthetic operation per tool; no synthetic tool_call behavior key downstream.

Core model deltas (all optional/nullable for compat; no new top-level model):

- ActionProposal: add tool_id (ToolId or None, default None). DIRECT iff tool_id is present. LEGACY iff tool_id is None and capability plus operation present. Keep capability/operation fields but treat them as optional metadata in DIRECT mode (hint/display only, never an execution precondition). Never invent capability equal tool or operation equal tool_call to satisfy old validators.
- ToolInvocation: change operation to (str or None, default None). DIRECT iff operation is None (tool_id present). LEGACY iff operation present. Validator: DIRECT requires proposal.tool_id; LEGACY keeps the current copy rule. Gateway calls supports() only when operation is not None.
- Selected-tool target semantics: DIRECT path validates TargetRef.kind against ToolDescriptor.target_types plus JSON Schema (input_schema, required/optional arguments) plus ToolDescriptor.argument_bindings plus network identity/alias binding. The OperationTargetBinding table, requires_url_target and binds_target_arguments flags, and execution-recipe operation match become LEGACY-only helpers; they must not gate DIRECT.
- Invocation identity: tool_id is the identity. action_id links approval; invocation_id links result/evidence; candidate/work key for DIRECT excludes operation (see section 5). The LEGACY operation path stays only for older APIs and checkpoints.
- Display/semantic key (no policy allow-list): add pure helpers, e.g. display_operation_key(proposal/step) returning the legacy canonical key when bound else direct:<tool_id>, and semantic_key returning (tool_id, target-kind, principal-class) for logs/graph/audit. Policy/control must not add a generic behavior allow-list entry matching the DIRECT marker; it evaluates registration plus scope plus argument risk plus the approval record.

## 4. Exact producer-to-execution-to-resume chain

Producer (fresh):

1. engine.context() plus _writing_schema plus execution_options: today behavior-filtered; DIRECT must expose registered tools by role scope (agent capabilities plus eligibility plus agent_tool_visible plus health plus schema), showing operations as-is (possibly empty).
2. ActionIntent (engine) to compile_actions (engine.py:1858-): today checks path membership, known target, role/capability, catalog.get(intent.tool_id), execution-recipe branch, canonical_action_key, principal, node/target build, outbound-text preflight (builds a throwaway ActionProposal), ambiguity/identity, PlanStep build, CandidateCompiler re-resolve, candidate_key hash, duplicate/legacy-identity admission. DIRECT keeps all except behavior gates; adds tool registration plus schema plus target-type plus eligibility plus normalized-args checks.
3. CandidateCompiler.compile_drafts_with_rejections: today behavior re-search; DIRECT adds a tool_id-verified path (catalog.get plus schema plus target-type plus health), skipping list_tools_for_action, bound_operations, and namespace match.
4. PlanStep to ActionProposal build (compile_actions tail): today drops tool_id; DIRECT must copy tool_id plus normalized arguments plus target plus reason verbatim.
5. policy_node: today validate_current_action then control_plane.evaluate(proposal, ControlContext(selected_tool_id from plan_step.tool_id)); REQUIRE_APPROVAL/DENY/ALLOW branching; resume branch uses approval_outcome. DIRECT: selected_tool_id must equal proposal.tool_id; decision persists action_id plus tool_id plus normalized-args digest; no behavior allow-list.
Execution:

6. core.convert.build_tool_invocation: today (proposal, decision, tool_id from step.tool_id); DIRECT asserts decision.action_id equals proposal.action_id and the tool match (param equals proposal.tool_id when DIRECT), and allows operation None through.
7. execute_node binding_ready plus _invoke_with_semaphore plus long-job receipt plus retry gate: today behavior equality plus healthy descriptor plus retry_safe/retry_policy(capability.operation). DIRECT replaces with tool-registration plus adapter ownership (namespace-free) plus health plus scope plus schema plus admission receipt (proposal plus plan_step plus invocation triple stored); retry predicate must use descriptor.retry_safe and argument-risk, not the behavior key.
8. gateway._invoke: today unknown_tool, unknown_server, supports(), dry_run, health probe, adapter call. DIRECT keeps everything except supports() when operation is None; registration/health/schema/scope/evidence mapping stay. Error codes stay distinct (unknown_tool vs operation_not_allowed only for LEGACY).

Resume:

9. Checkpoint (LangGraph) holds RuntimeState.pending_action_id/pending_approval_id plus GraphState plan_step/action_proposal/policy_decision; outbox/evidence hold the submission receipt. DIRECT requires the triple (proposal.tool_id, step.tool_id, invocation.tool_id) persisted together.
10. resume CLI to read_checkpoint_identity to binding_for_resume to build_application to recover_runtime (refuse EXECUTE/WORKER-pending) to resume_recovered to _register_pending_checkpoint (register_pending_proposal) to _resume_pending_approval re-entering at POLICY. DIRECT adds tool-equality asserts at each link; mismatch fails closed, never falls back to behavior search or audit reconstruction.
11. policy resume: pending_action_id equal proposal.action_id gate stays; add proposal.tool_id equal stored tool check; approval_outcome decision must carry the same action_id plus tool_id.

## 5. Fresh/resume check list (DIRECT vs LEGACY)

- Fresh compile: path-step membership, target known/observed, agent exists (role check without behavior match when DIRECT), tool registered, schema valid, target-type match, eligibility role/tool/action pass, outbound-text preflight bound to tool_id, ambiguous-execution/identity (DIRECT key hashes tool_id, normalized args, targets, principal, knowledge fingerprint), expectation/hypothesis linkage. Skip: canonical_action_key, bound_operations, list_tools_for_action, operation_target_binding gating.
- Admission (validate_current_action): proposal equal step on tool_id (DIRECT) plus target/arguments/availability; operator epoch; principal args; schema plus target-type plus eligibility; execution-recipe equality only for LEGACY operation recipes.
- Execute: triple tool equality, lease, admission claim, long-job receipt with DIRECT key, bounded retry on gateway-retryable plus LOW-risk descriptor plus argument-risk predicate.
- Resume: checkpoint triple equality, pending_action_id plus pending_approval_id match, stored component binding, approval link (action_id plus tool_id), unsafe-replay refusal, evidence receipt presence for legacy-identity conflicts.
- LEGACY-only (old APIs/checkpoints): canonical_action_key, bound_operations, supports(), operation_target_binding table, recipe operation match, no_tool_for_operation and operation_not_allowed_for_tool codes. Keep behavior, do not extend.
## 6. Role manifests, catalogs, skills/MCP exposure, config-file registration

- Role manifests (existing pluggy, no new kind needed): orchestration/agents/packages.py RolePackage (definition plus spec plus provider link plus default flag), kinds agent_role plus candidate_provider; capability/registration.py mcp_profile bundles plus CapabilityFactoryContext; orchestration/eligibility.py eligibility_provider; app/extensions.py collect_extensions plus ExtensionAssembly; app/bootstrap.py wires gateway/catalog into RuntimeDeps. DIRECT reuses the same collection: a role bundle proposes tool_id directly; provider/eligibility contributions stay domain-owned; the central loop keeps no vendor/behavior branches.
- Current catalogs: capability-owned MCPToolCatalog (registration snapshot plus resolved operations plus health via registry) behind the core ToolCatalog port (get/list_by_capability/list_tools_for_action); orchestration candidates/catalog.bound_operations is a compat filter. DIRECT adds a get-by-tool_id plus health plus schema read path alongside; behavior-listing APIs stay LEGACY-only. Note: eligibility _for_tool skips behavior-scoped providers when tool.operations is empty, so DIRECT must apply scope providers by server/capability scope, not operation intersection.
- Skills/MCP exposure: no separate skills registry was found in src (unconfirmed beyond adapters/manifests/servers); exposure is MCP adapters plus capability manifests/servers plus orchestration candidate providers plus the engine context/tools dict. DIRECT must list registered tools by role/eligibility scope, not by behavior membership.
- Config-file field registration: possible but not required. Today CapabilityConfig (core/config.py:187-189) carries only mcp_servers (plus methodology); per-tool bindings come from env (MCPServerSettings tool_operations/allowed_tools) plus profile bundles plus role specs. A file field (e.g. optional capability.tool_bindings passed into the same gateway/catalog operation_bindings map) is a compatible assembler input; it must not become per-tool central branches. Prefer reusing env/profile/role tables unless operators explicitly need file-pinned bindings.

## 7. Package boundaries (actionable)

- Capability owns: registration snapshot, operation_bindings map (LEGACY), get-by-id plus health plus schema truth, gateway supports() LEGACY-only, adapter routing, target_types/argument_bindings declaration shape. Must not own behavior-choice or approval.
- Core owns: ActionProposal.tool_id optional, ToolInvocation.operation optional, build_tool_invocation tool-match guard, display/semantic-key helpers, ToolCatalog/ToolGateway port shapes (additive only).
- Orchestration owns: context exposure by role scope, compile/admission/execute triple-tool checks, candidate/work keys, checkpoint triple, resume re-link, eligibility wiring. Must not branch on tool names or synthesize behavior keys.
- Control owns: evaluate/admit/approval_outcome bound to action_id plus tool_id plus normalized args; risk/scope/argument checks; no generic DIRECT allow-list.
- App/composition owns: collecting existing pluggy declarations, feeding optional file-field bindings into the same maps, building gateway/catalog/deps. No new framework/store.

## 8. Unconfirmed / root follow-ups

- Exact ActionIntent required-ness today (observed mandatory behavior plus tool per early memo; re-verify against the current engine dataclass) and whether capability/operation can be made Optional without breaking persisted path/completion schemas.
- Whether old checkpoints (no tool_id) load as LEGACY while new DIRECT checkpoints refuse downgraded readers loudly.
- Skills surface: confirm there is no out-of-src skills catalog that also filters by behavior before touching exposure.
---

## Addendum A (2026-10-05 supplement): 분야별 등록목록 명시 파일 - correction to section 6

Correction: the closing sentence of section 6 in this report (file-pinned bindings only if operators explicitly need them) is withdrawn. The user explicitly requires 분야별 등록목록 명시 파일 via the existing registration module, so the plan MUST deliver an actual role-manifest config input plus defaults plus assembly plus resume digest. It is a required deliverable, not an optional future. What follows is the concrete simplest shape found in the current code.

### A.1 Precedent: MethodologyConfig is the file-field pattern to copy

- core/config.py MethodologyConfig (workspace_policy_path, bundle_policy_path, prefix, reference_ids 1-3, read_chars, max_context_chars) is the explicit operator-owned reference deployment that lives in a config file: CapabilityConfig carries mcp_servers plus methodology, parsed from yaml by load_app_config, with an env JSON mirror via MethodologyConfig.from_environment. The role manifest copies exactly this dual path: file field primary, env JSON mirror for parity.
- EngagementConfig.metadata (core/config.py:74, free dict) already exists as a per-engagement carrier, but it cannot drive assembly alone: durable resume rebuilds from the component registry, not from engagement content. So engagement metadata may carry a manifest ref as an override, but the composition root must resolve it into a registered profile plus pinned digest at build time.

### A.2 Simplest concrete fields

New optional frozen model on CapabilityConfig (beside methodology - roles plus tools are a capability/assembly concern, and load_app_config already parses that section from yaml):

- role_manifest_path: path to the manifest file (same 1-4096 char bounds style as MethodologyConfig paths)
- role_manifest_sha256: expected hex digest of the file bytes (integrity pin; mismatch fails loudly before any transport opens)
- only if operators need multi-profile: role_manifest_profile selection string; otherwise the manifest's own profile id/version inside the file is the key

Manifest file content (JSON or YAML, validated by schema in the capability/registration module - same role as validate_tool_manifest and validate_configured_discovery):

- profile: profile_id plus version (e.g. operator-roles with a date or semver version)
- roles: list of agent_id, contract_version, capabilities, provider_id, required_providers, optional_providers, default_include - mirrors RolePackage (agents/packages.py) plus AgentDefinition scheduler fields (agents/definition.py)
- tools: list of tool_id, target_types, argument_bindings - the provider-declared target semantics from A.4, validated against live discovery; discovery differing from the pinned table fails, same rule as validate_configured_discovery in registration.py
- defaults rule: field absent (None) means builtins exactly as today (builtin_role_packages with default_include, builtin mcp profiles). A manifest overrides per agent_id; unknown agent_id, unknown tool_id, or version mismatch fails at resolve_selection time - never silent fallback, same rule as unknown profile id/version.

Load path (mirrors the methodology plus component resolution order in app/runtime_components.py and app/bootstrap.py):

1. load_app_config yaml to AppConfig.capability.role_manifest, plus env JSON mirror REDTEAM_ROLE_MANIFEST for parity (same shape as MethodologyConfig.from_environment).
2. Composition root reads plus hashes plus schema-validates the file BEFORE any MCP transport opens (same position as resolve_selection in bootstrap.py around lines 278/528 and runtime_components resolve_selection).
3. Manifest feeds the existing assemblers only: ComponentRegistry.register(ComponentProfile) for a new operator profile (or selection of an existing one) plus ExtensionAssembly contributions of existing kinds (agent_role, candidate_provider, mcp_profile, eligibility_provider via collect_extensions plus apply_extensions plus validate_references/validate_kinds). No new contribution kind, no central per-tool branches. ComponentFactoryContext (llm_client, evidence_store, graph_store) stays unchanged - the manifest is consumed at selection time, preserving collection-vs-execution separation.
### A.3 Assembly plus resume digest (pinned snapshot in binding)

- RuntimeComponentBinding today (orchestration/component_binding.py) records profile_id, version, component_ids for the 4 slots, extension_versions, extension_entry_points, planning_provider_id; binding_for_resume fails on unknown ids, changed ids, or extension_versions mismatch (runtime_components.binding_for_resume; resolve_binding raises ComponentMismatchError). The checkpoint carries component_binding (checkpointing.py around line 499; RuntimeState.component_binding; the resume CLI checks the stored binding before opening transports).
- Required addition (additive, default None so old checkpoints load as legacy/builtin): one optional field on RuntimeComponentBinding, e.g. role_manifest_digest, holding manifest_sha256 plus roles_table_hash plus tools_table_hash. instantiate/bind_extensions records it; binding_for_resume compares it and raises ComponentBindingError on mismatch (fail closed, same rule as changed component ids). This is the resume pin: a run resumed against a different manifest is refused, never silently rebuilt.

### A.4 DIRECT-call target semantics are provider-declared, not operation-only

- Owner today: ToolDescriptor.target_types plus argument_bindings plus input_schema are declared by the tool provider (adapter/manifest/discovery; see tool.py descriptor fields and the registration.py apply_server_catalog_target_types precedent that copies pinned per-tool target hints into the gateway catalog map). The OperationTargetBinding table (core/provider_contracts.py) keyed by operation string is the legacy overlay.
- Rule for the plan: DIRECT execution reads target semantics ONLY from the pinned provider-declared table (descriptor.target_types vs TargetRef.kind plus argument_bindings plus schema plus network alias binding). The operation-keyed table stays legacy-only and must not gate DIRECT. The manifest tools table from A.2 is the file-pinned source of those provider declarations, validated against discovery and consumed generically (no per-tool central branches).

### A.5 What the plan must contain (checklist for root)

1. Config fields (CapabilityConfig.role_manifest_path plus role_manifest_sha256 plus env mirror) with bounds plus sha pin.
2. Manifest schema plus validator in the capability/registration module plus discovery-vs-manifest mismatch error.
3. Defaults rule (None means builtins; per-id override; unknown id fails at selection).
4. Registry/assembly wiring reusing existing kinds (no new kind).
5. RuntimeComponentBinding.role_manifest_digest plus resume comparison (fail closed).
6. DIRECT target checks reading only the pinned provider table.
