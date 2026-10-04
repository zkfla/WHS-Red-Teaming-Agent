> 이 문서는 작성 당시의 감사·검토·작업 기록이다. 중간 상태와 검증 수준을 그대로 보존한다. 최신 구현·CI·병합 상태는 [목차](../README.md)의 최종 계획을 기준으로 읽는다. 인용 행 번호는 검토 당시 기준이며 소스 링크에 현재 행 번호를 추정해 붙이지 않았다.

# state-control impl-A note (worker A)
date: 2026-10-04 Asia/Seoul
base: 9eb70ed1f9fff1fbf15540731de87de89feaf7fe
status: code done, tests NOT run (root test ban)

## 1 changed files
- src/redteam/control/budget.py : add duration_exhausted
- src/redteam/control/service.py : add _recheck_time_gates, _in_flight_from_context, admit_invocation, reserve_extra_tool_call, _finalize dual-write severity, evaluate typed in_flight
- src/redteam/orchestration/observations/recon_http.py : safe host/port/scheme parse, IPv6, port0 reject, effective port
- src/redteam/attack_graph/identity.py : endpoint/api bracket IPv6 when port present
- src/redteam/attack_graph/stores/namespaced.py : edge id namespacing whole-id prefix, legacy lazy adopt, list/get/remove
- src/redteam/orchestration/evidence_access.py : provenance_of, classify_raw, read_raw_record, EvidenceReader provenance additive, guarantee docstring
- no-git/test_patch_tmp.txt : scratch, remove before review (not product)

No changes: core, bootstrap, engine, execute, candidates, other mappers, service.py export/import, completion.py, knowledge.py, recovery.py.

## 2 new API and behavior
- BudgetTracker.duration_exhausted(now=None): time-only, no count touch, no anchor reset. None cap or unstarted -> False.
- DefaultControlPlane._recheck_time_gates: shared ROE time gate via check_roe, maps to roe_window_closed, roe_parallel_exceeded, availability_blocked. UnsupportedRoe -> roe_window_closed ABORT.
- DefaultControlPlane._in_flight_from_context: typed ControlContext.in_flight_actions first, legacy metadata fallback. Fallback owned here.
- admit_invocation(proposal, decision, context, receipt_id): invariants fail-closed no charge, scope -> ROE UTC now with typed in_flight -> duration only. No has_capacity, no rate would_allow allow, no budget consume, no approval request resolve, no pending pop. Receipt idempotent process-local. DENY via _finalize once, ALLOW no audit. Reasons: decision_mismatch scope_revoked roe_window_closed roe_parallel_exceeded availability_blocked duration_exhausted.
- reserve_extra_tool_call(proposal, decision, context, receipt_id, attempt_index): attempt>=1, same invariants, tool_calls_remaining + duration + ROE time availability, consume_tool_calls exactly once, no rate action touch, no parallel recheck. Receipt idempotent. Reasons: decision_mismatch tool_calls_exhausted duration_exhausted roe_window_closed availability_blocked.
- recon_http: target host:port and output host parsed with urlsplit plus bare IPv6, hosts_equal lower plus ip equality, port0 and bad range reject, scheme http https only, effective port output else target, mismatch deny, graph keys use normalized host and effective port.
- identity: endpoint_node_id and api_resource_node_id bracket bare IPv6 when port present. No change for no-port to limit compat break.
- namespaced edge: write prefix whole edge id with ns US, read strip, get remove convert request first, legacy plain id with owned endpoints lazy adopted by copy-add plus old-delete best effort. Overwritten relations not restored.
- evidence: provenance_of record fields, classify_raw new vocab ok missing other_run no_raw, read_raw_record unwindowed never truncated, EvidenceReader.read keeps old statuses not_found run_mismatch no_raw ok plus additive provenance, truncated stays bool on ok. Guarantee docstring main VERIFY full raw vs convenience digest-only must not count for completion.

## 3 root patch requests
### 3.1 core ports control_plane declarations (MISSING, need root)
File: src/redteam/core/ports/control_plane.py
After approval_outcome block, add:
  async def admit_invocation(self, proposal: ActionProposal, decision: PolicyDecision, context: ControlContext, receipt_id: str) -> PolicyDecision: ...
    Re-check before invoke, no charge, receipt idempotent.
  async def reserve_extra_tool_call(self, proposal: ActionProposal, decision: PolicyDecision, context: ControlContext, receipt_id: str, attempt_index: int) -> PolicyDecision: ...
    Retry extra one call atomic, receipt idempotent.
Need imports: ControlContext PolicyDecision already present, no new types.

### 3.2 core contracts policy already applied by root (CONFIRMED)
File: src/redteam/core/contracts/policy.py
- ControlContext.in_flight_actions: int None ge0 already present. Our code uses typed first, metadata fallback.
- PolicyDecision.severity: Severity None already present.
- PolicyDecision.denial_severity property plus legacy_denial_severity already present.
No patch needed. Disposition name in request maps to severity in code.

### 3.3 severity dual-write decision (worker A ruling)
- _finalize and reserve deny set typed severity plus legacy severity tag via to_tag.
- Keep tag writing: tag tests and orchestration readers still rely on matched_rules tags. Typed alone would break old records and tag tests.
- Reading uses PolicyDecision.denial_severity property or legacy_denial_severity, no new parser in control. orchestration nodes policy parser should migrate to core helper by root.
- Evidence: tag tests exist for matched_rules tags, typed field new. Dual-write is only compat path.

### 3.4 execute wiring (need root, file owned by root)
File: src/redteam/orchestration/nodes/execute.py
A) in _invoke_with_semaphore invoke(), after claim_action_admission, before tool_gateway.invoke, add admit_invocation non-charging recheck with receipt per invocation. On DENY raise or convert to ToolResult error, do not invoke.
Context lines:
      problems = await validate_current_action(deps, runtime, step, proposal)
      if problems: raise ValueError
      await claim_action_admission(deps, runtime, step, proposal)
  Patch insert after claim:
      admit = getattr(deps.control_plane, admit_invocation, None)
      if callable(admit):
        ctx = ControlContext with engagement agent correlation and in_flight typed plus metadata fallback
        adm_decision = await admit(proposal, decision, ctx, receipt_id=str(invocation.invocation_id))
        if adm_decision.verdict != ALLOW: raise or return DENY ToolResult, no invoke
B) retry loop replace pair:
  Old:
      reserve = getattr(deps.control_plane, has_tool_call_capacity, None)
      charge = getattr(deps.control_plane, record_tool_calls, None)
      ...
      async with deps.policy_lock:
        if not await reserve(proposal.engagement_id): break
        await charge(proposal.engagement_id, 1)
  New:
      reserve_extra = getattr(deps.control_plane, reserve_extra_tool_call, None)
      ...
      async with deps.policy_lock:
        r = await reserve_extra(proposal, decision, ctx, receipt_id=f"{invocation.invocation_id}:retry:{retry_attempts+1}", attempt_index=retry_attempts+1)
        if r.verdict != ALLOW: break
  Keep runtime.remaining_budget local guard as is. has_tool_call_capacity record_tool_calls stay for compat only.

### 3.5 engine register_path F02 correction (need root)
File: src/redteam/orchestration/engine.py around register_path step loop after completion criteria check.
Add after typed criteria check:
  for step in path.steps:
    if step.required (or equivalent flag):
      ops = list(step.allowed_operations or [])
      if not ops or all(op not in EXECUTION_OPERATIONS for op in ops):
        raise DecisionRuleError(correction with executable operation required, code invalid_arguments)
Rationale: required summary-only steps can never produce step evidence, completion will later reject with different_step. Reject at registration with correction to normal Task Goal flow. No new aggregation kind receipt lifecycle. completion.py untouched.

## 4 tests
- NOT RUN per root ban. No new test files added yet.
- Planned minimal: control admission reserve max1 rate1 duration, edge two-engagement same relation, recon IPv6 hostport, evidence provenance classify.
- Existing related to run last: control resume accounting, approval outcome, budget rate roe scope, graph tenancy sqlite, recon mappers, evidence.

## 5 limits and unconfirmed
- admit ALLOW does not audit to avoid duplicate, DENY audits once per receipt. Replay returns cached without recheck, so config change after first receipt is not seen on same receipt. New receipt needed for fresh check.
- duration uses process monotonic via budget, UTC for ROE. Resume rehydrate monotonic anchoring unchanged.
- reserve in_flight=0 forces parallel pass. check_roe with 0 passes any valid max_parallel_actions>=1, so retry never blocked by parallel. If max_parallel were 0 invalid config, would still deny, but schema ge1 forbids. Note for review.
- in_flight typed None plus metadata missing with max_parallel configured fails closed in admit via check_roe, same as evaluate. Rate concurrency None stays not applied per RateLimiter contract.
- edge legacy adopt best effort, two-step add then delete not atomic. Concurrent adopt may briefly duplicate but converges. Overwritten pre-fix relations lost, not restored. SQLite restart adopts on first read after setup. Whole-id prefix format chosen over endpoint-embedded for parse safety. Old plain ids with owned endpoints treated as legacy.
- identity bracket change only affects IPv6 with port. Old ambiguous keys like endpoint ::1:8088 without brackets will not match new bracket keys. No auto migration for graph node keys. recon normalizes to new form.
- evidence new vocab missing other_run maps to old not_found run_mismatch in Reader output for compat. knowledge recovery completion untouched, so their own run_mismatch no_raw handling remains duplicated. Truncated status in new vocab maps to ok plus truncated True in windowed Reader.
- server output host containing port is defensively accepted, not a server fix. Port pinning deletion still denied via mismatch.

## 6 source vs doc
- findings 15.3 reserve name reserve_extra_tool_call used, not reserve_tool_call. Done.
- findings 4.3 included rate non-consuming check in admit, but 15.3 forbids rate. Followed 15.3 no rate.
- findings 6.2 migration example endpoint-embedded, implemented whole-id prefix for safety. Behavior same isolation, format differs. Noted.
- findings 8.2 read_raw evidence invocation args, implemented evidence run based plus provenance to keep behavior identical. No lineage schema change.
- control_plane.py declarations and policy typed fields were root pending at task start, now policy typed present, control_plane declarations still missing. Adjusted to actual source.

## 7 root review reflection
- root review: admit no-cache and current-check every call, reserve charge idempotent by key with checks-first order.
- admit: _admit_receipts removed. Every admit_invocation runs invariants then scope then ROE now UTC with typed in_flight first plus metadata fallback then duration from scratch. receipt_id is caller correlation only, never storage or short-circuit. DENY is recorded through _finalize per call, never reused from storage.
- reserve: _reserve_receipts value cache removed. Replaced with _consumed_reservations set keyed by engagement_id action_id receipt_id attempt_index. Every call order is invariants then current ROE duration tool-call residue checks, then if key already consumed and current checks pass ALLOW without consume, else consume_tool_calls once then record key then ALLOW. No stored decision is returned before current checks.
- signatures kept: admit_invocation(proposal, decision, context, receipt_id) and reserve_extra_tool_call(proposal, decision, context, receipt_id, attempt_index). Matches planned InvocationAdmissionControl optional Protocol shape for nodes execute wiring.
- reserve in_flight=0 comment states retry creates no new parallel slot. check_roe with 0 passes any valid max_parallel_actions>=1, so with max_parallel_actions=1 the retry gate passes while a genuinely parallel other action would already have been denied at admission. Parallel is owned by admission, not retry.

## 8 root re-review reflection 2
- reserve order defect fixed: capacity is checked only for new keys. Shared current checks run first, then consumed-key check returns ALLOW without capacity or charge when the same attempt was already consumed and current checks still pass. New keys check tool-call residue once, then charge once and record the key. DENY is never stored or short-circuited.
- shared current check extracted as _current_admission_check covering receipt, decision action verdict, engagement run, approval APPROVED read, scope, ROE time with live others, and duration. admit_invocation and reserve_extra_tool_call call the same function. Reserve keeps only atomic reservation logic after it. Scope missing in reserve is fixed by sharing.
- in_flight=0 forcing removed. Shared check uses _in_flight_from_context typed first plus legacy metadata fallback, meaning the live others count. The retry comment keeps that a retry makes no new parallel slot of its own, but a saturated parallel gate still denies instead of being bypassed.
- signatures unchanged. Note file only, no test run per ban.
