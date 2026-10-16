# Owned provider and shell concurrency

## Scope

Continue in the isolated `codex/execution-receipt-recovery` checkout from
`b100d716`. The user authorized implementation, offline verification and atomic
commits. Integration, push and live validation remain with the parent. No live
target, model, MobSF, ADB or Docker calls are part of this verification.

This extends the provider scope of `mcp-termination-and-parallel-capacity.md`.
Independent work may overlap, while shared resources, UNKNOWN execution fences,
Control, approval, scope, ROE and budget constraints remain enforced.

## Ownership and resource boundaries

| Provider or substrate | Concurrency and retained serialization |
| --- | --- |
| Recon and web HTTP | Existing bounded `ServerPolicy.max_concurrency`; request-local clients and response cleanup. |
| Active and Nuclei | Same policy bound replaces whole-call locks; subprocess and temporary output belong to each call. |
| Proxy | Same policy bound plus canonical origin/path locks across replay, mutation and the complete ordered comparison pair. Principal/session aliases share the physical path. Discovery publishes the same identity for durable scheduling. |
| ZAP | Same policy bound; read-only HAR export uses a request-local client and parameters. |
| Mobile | Default-1 policy bound plus actual ADB serial locks. Waiting for the same device does not occupy a provider slot. |
| MobSF | Default-1 policy bound plus backend content locks. SQLite transactions reserve uploads and claim scans before remote calls, including across clients sharing state. Unsettled outcomes retain the content fence. |
| VDB | Lookup runs in a fresh process so upstream module/environment globals are isolated. Policy defaults to one lookup; a finite process-wide worker ceiling also applies. Status uses its own immutable database handles. |
| Shell adapter | Short atomic job reservation precedes external preparation. Spare capacity admits new jobs without first probing unrelated UNKNOWN jobs. Duplicate submissions wait on the same reservation and never spawn again. |
| Shell workroom | Existing/adopted rooms serialize by workroom key. The cross-process creation lock remains around shared workroom-cap accounting and creation. |
| Execution server | Existing job lock covers short dictionary/capacity transitions; execution and cancellation waits are already outside it. |
| Code fixtures | Existing finite `max_jobs` semaphore is a capacity limit; separate fixture calls have separate workrooms. |
| Workspace | Shared-root writer `flock` protects mutation, quota checks and atomic writes; readers do not take it. Retained deliberately. |
| Shared shell preparation | Cross-process preparation lock protects image/target startup and restore. Retained deliberately. |
| Kali HTTP and install proxy | Short ordinal-journal and statistics locks; execution is outside them. Retained deliberately. |
| Analysis, inspection, search and page text | No whole-call provider lock to remove; existing resource descriptors, path validation and per-call state remain authoritative. |

## Configuration and compatibility

Provider capacity, gateway server capacity, worker/global/per-target capacity,
resource conflicts, Control and ROE are independent upper bounds. Effective overlap
is limited by all of them. Increasing one setting cannot bypass the others.

The approved deployment generator currently provisions web HTTP and passive recon.
For those roles the approved `ServerPolicy.max_concurrency` supplies generated
connection settings, verification and provider capacity. Other launcher roles keep
their existing explicit policy-file and `MCPServerSettings.max_concurrency` inputs;
operators must configure both when requesting overlap. No new deployment role or
central provider-name switch was added.

New policy concurrency fields preserve historical default serialization by omitting
the default value of one. Existing run/deployment fingerprint checks remain in force.
Finite limits need not be equal across providers.

Proxy discovery metadata is static for a running MCP server. A newly added flow
after capture reload fails closed in durable resource resolution until the provider
restarts and discovery refreshes. Existing refs remain bound to their original
physical path. No inferred resource identity is substituted for an unknown ref.

MobSF owns an operator-selected private state database. Upload uncertainty consumes
quota and blocks another submission for that content; cancellation of the caller
does not prove that the remote scan stopped. No remote cancellation or termination
receipt is fabricated.

MobSF discovery derives durable identity from endpoint and approved SHA, avoiding
eager reads of every APK at discovery. Runtime locks and persisted scan fences use
the actual backend MD5. Every post-reservation HTTP failure, including 4xx, retains
the reservation because a no-effect guarantee is unavailable. Separate clients
for one engagement/run must share the configured state database.

## Verification

Tests use explicit local transports, subprocess fixtures and model doubles; they
are not live service validation. Python 3.14.6 uses the authorized interpreter with
`PYTHONPATH` pointing to this isolated checkout; Python 3.10.4 runs static tools.

- Active/mobile/MobSF/Nuclei/ZAP/proxy/workroom selection: 73 passed.
- Shell adapter and workroom concurrency/lifecycle regressions: 98 passed.
- Shell recovery/evidence, built-in gateway, code fixtures, preparation and execution
  environment resume: 118 passed. An existing 65 KiB parameter-derived pytest ID
  exceeded Windows' environment-variable limit; explicit short test IDs fix only
  the test harness and preserve both validation cases.
- Config-to-gateway barriers for all seven provider policies (default 1 and explicit
  6), plus execution-server regressions: 44 passed.
- Actual proxy provider through generic MCP adapter/bootstrap/gateway: six distinct
  paths overlap while a seventh queues; same-path calls serialize. Provider cap 7
  versus gateway cap 6 proves that the gateway bound is independently enforced.
  Proxy focused suite with this integration: 13 passed.
- Control/ROE, parallel receipt/model ledger, termination/reconciliation, deployment
  and independent resume selection: 91 passed, one existing POSIX permission-bit
  assertion fails on Windows. Its checks were not weakened.
- VDB read-only selection before cleanup-review follow-up: 26 passed, 13 opt-in
  backend tests skipped, two symlink-privilege tests deselected on Windows.
  Real OS fixture processes prove overlap, distinct PIDs, environment isolation
  and timeout kill/reap; the actual module worker exercises missing-snapshot failure.
  Actual AppThreat/SQLite search needs the separately provisioned opt-in backend.
- Extended-VDB tests on this Windows host: 37 failures at existing WorkspaceIO root
  setup. A representative constructor fails identically with `b100d716` and current
  VDBReader, loaded separately without working-file swaps (`vdb_invalid_sbom_root`).
  These are not claimed as an all-green full-suite run.

## Final review corrections

- VDB termination waits and pipe-thread joins share a bounded 0.5-second cleanup
  budget. No unbounded wait remains. If the child or pipe threads are not proven
  stopped, `vdb_lookup_cleanup_uncertain` retains both the reader's policy slot and
  the process-wide slot. A background reaper retains the child and pipe-thread
  handles, checks actual termination and returns both owned slots exactly once
  when all have stopped. It never retries the lookup or releases on elapsed time.
  Active pipe handles are not synchronously closed. Tests
  cover an unending process wait and stuck reader/writer threads, plus real child
  kill/reap. Final focused VDB run: 28 passed, two Windows symlink cases deselected.
  Cold worker startup measured 2.29–3.06 seconds here and counts against the default
  five-second lookup deadline; it is not excluded from accounting.
- MobSF caller cancellation leaves the remote scan row `running`, because no remote
  cancellation route was invoked. Restarted clients retain the same-content fence;
  missing or mismatched reports do not complete it, while a matching report can.
  Final MobSF/finance selection: 45 passed. Combined final wiring/proxy/MobSF check:
  51 passed.
- Final CI-scope Ruff and `git diff --check` pass. Linux-platform mypy passes all
  928 source/test files. No permission assertion, Control rule or live-call boundary
  was relaxed. Scope is complete for implementation and the stated offline checks;
  actual AppThreat backend and live integration remain explicit external validation.

## Follow-up: reclaim delayed VDB termination

Code review found that a transient cleanup delay permanently consumed the held
slots. The cleanup diagnostic now carries a termination probe retaining the actual
child and pipe-thread handles. Lookup transfers the exact acquired semaphores to
a bounded registry before unwinding; one background thread polls those records
without waiting on the child or pipe threads. A record releases both slots once,
only after child exit and both pipe threads have stopped. Failed reaper startup
keeps the record and is retried on later calls. No lookup is replayed and no result
is changed to success. Regression checks cover delayed child/reader/writer exit,
subsequent lookup success, repeated proof, and isolation from another reader's slots.
Validation: 29 focused tests passed; two Windows symlink-privilege cases remained
deselected. Scoped Ruff, Linux-platform mypy and `git diff --check` passed.
