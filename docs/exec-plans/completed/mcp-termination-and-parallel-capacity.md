# Returned MCP termination and configurable parallel capacity

Status: COMPLETE for scoped implementation and offline verification

## Scope and authority

Continue on isolated `codex/execution-receipt-recovery` from `a6a8ef67`.
Do not change the original checkout or live fixtures. No target/model/Docker calls.
User authorizes atomic commits; integration/push/live validation belong to the parent.

The reported recon HTTP error now correctly remains UNKNOWN, but lacks a recovery
reference and consumes a global slot. Outcome uncertainty is not provider liveness.
Determine whether explicit awaited local cleanup can prove the latter. Do not infer
termination from an MCP return or fabricate a recovery handle.

The user also authorizes finite configurable parallelism. Three is an example, not
a runtime ceiling. Preserve worker/global/specialist, Control, ROE, per-target and
model-ledger bounds, with scope, approvals, resource conflicts and shared budget CAS.

## Work and ownership

- Root: original-result termination receipt, immutable correlation, state reconciliation.
- Provider worker: bounded awaited HTTP cleanup proof and explicit MCP metadata bridge.
- Independent reviewer: local versus remote termination semantics, negative cases.
- Accounting worker: trace and correct proven no-progress accounting omissions.
- Settings worker: validated configurable lab profile and injection/precedence checks.
- Integration workers: actual overlapping tool/model calls, immutable evidence and
  later cross-worker context, UNKNOWN remaining capacity and safety gates.

## Acceptance and limits

No detached provider work may be certified. Missing cleanup, timeout, cancellation,
lost result, wrong identity, missing original or output-only assertions remain held.
Termination releases capacity only; UNKNOWN, resource fences and no replay persist.
No-progress cost changes must be grounded and idempotent across restart, not a blanket
ban on useful local evidence work. Tests use explicit fake models/transports and real
ledger/evidence/SQLite paths where relevant. Provider serialization must be inspected
instead of merely raising numbers. No unconditional default concurrency expansion.

## Validation

- Root draft original-result proof: existing termination/reconciliation tests 9 passed;
  central recovery and independent-resume regressions 18 passed (Python 3.10.4).
- Root changed core/receipt/state modules: Ruff and Linux-platform mypy passed.
- New provider, forged proof, cost accounting and parallel overlap tests pending.

## Confirmed implementation boundaries

- Historical originals without explicit termination metadata cannot release capacity.
- MCP transport timeout/cancellation is not a returned termination assertion.
- `OrchestrationSettings.from_config` omitted global/per-target concurrency, leaving
  defaults 4/1 even when CLI max_workers increased. Configuration mapping must be fixed.
- Gateway per-server limits and role-generated deployment environment default to 1;
  built-in recon/web HTTP servers also hold one lock across the whole request. Every
  bound must be explicit and consistent before claiming parallel throughput.
- Adding a provider policy concurrency field must omit its historical default from
  canonical serialization to preserve existing deployment bytes and hashes.

## Implementation and review decisions

- Original-result termination receipts bind the submission and exact immutable result
  ID/digest; state rereads both, and rejects changed result pointers. No handle is invented.
- Generic MCP adapters ignore termination assertions by default. App assembly opts in
  only roles declared by a verified local deployment and provider-owned role contract.
  Output DATA, discovery metadata and environment values cannot grant this trust.
- Review found HTTPX marks streams/clients closed before awaited cleanup finishes, and
  httpcore may remove failed pre-header connections from its pool. Therefore pre-header
  errors remain held even if client close returns. Only independently observed first
  response-stream close plus first client close can prove termination. Cleanup is bounded,
  awaited in the request task, and does not launch a detached task.
- UNKNOWN/FAILED native decisions settle no-progress independently from execution closure.
  Useful completed siblings can make a batch progress; unattempted siblings keep its
  progress unclassified. No false completion or blanket planner suppression is introduced.
- Global/per-target config now reaches runtime; explicit resume overrides are checked by
  the existing Run binding. The existing run --workers indirect override was confirmed
  working and was not changed. Limits remain finite and independently configurable.
- Approved ServerPolicy.max_concurrency drives deployment env, verifier, gateway and
  recon/web HTTP semaphores; default 1 is omitted from canonical JSON. Kali assembly
  consumes the same approved limit. Central scope, ROE, approvals and resource conflicts
  are unchanged. Other stateful providers' independent locks are out of scope.
- Six-task tests assert actual concurrent execution, persisted distinct receipt IDs,
  UNKNOWN plus five peers, and producer original ID in a later consumer's real assembled
  model input and published result. SQLite model-ledger tests exercise caps 3, 4 and 6.

## Final verification record

- Python 3.14.6: termination/provider/adapter regression selection, 92 passed.
- Final parallel execution/provider and app wiring selection with explicit ROE cap 6:
  15 passed. New SQLite model ledger tests cover 3/4/6 and passed in the combined run.
- Network recheck after type narrowing: 31 passed.
- Parallel/model/progress/config/deployment selection: initially 72 passed, 5 failed.
  Four failures were new test SQLite reads not explicitly closing their connections;
  fixed those test handles, then progress plus changed resume-binding cases: 9 passed.
  The remaining existing test checks POSIX mode bits on Windows and remains a platform
  limitation; no permissions assertion was relaxed.
- Linux-platform mypy: 924 source files passed. CI-scope Ruff passed.
- Broad worker-run legacy central_parallel tests under concurrent Python 3.10 load had
  timeout/Windows SQLite cleanup failures. Its isolated model-cap test passed on 3.14.6.
  These are not claimed as an all-green full-suite run.
- No live target/model/Docker execution, integration or push. The old live original has
  no termination flag and cannot be retroactively released; pre-header failures stay held.
