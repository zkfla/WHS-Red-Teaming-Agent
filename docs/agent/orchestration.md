# Current orchestration runtime

The default application runs a main planner and bounded native workers over
`data/orchestration/state.sqlite3`. The configured default is two workers.
The specialist profile and shared LLM request policy must be selected explicitly;
missing configuration is an error, not a switch to a test double.

## Flow and ownership

```text
operator goal + criteria + scope
    -> main planner: propose investigation questions and Tasks
    -> central Task registration and atomic owner/generation claim
    -> InstanceDriver: independent SDK thread per worker instance
    -> MODEL -> TOOLS -> SETTLE
    -> Control admission -> original action/invocation -> provider
    -> immutable evidence -> execution + graph + result transaction
    -> committed notification -> planner re-evaluation / criterion review
```

A Task is an investigation purpose, not a queue entry for each tool. Its role,
subjects, original evidence references and result belong to the central row.
Task ID, role ID and instance ID are separate. Multiple workers may share a role
or investigate the same evidence. Only replay of the same original registration
or execution identity is idempotent; changed content under that identity fails.

The planner makes strategy/atomization judgments without provider tools. Each
worker chooses tools through the actual role/catalog contracts. Registration,
readiness, permission, successful execution and a supported conclusion are
separate checks. Evidence/tool output is data, never an instruction or approval.

`CentralRun` owns the planner and `InstanceDriver`. The driver bounds and drains
actors and isolates SDK continuations. `central_state` owns Tasks, owner generations,
executions, outbox, operator intents and shared model accounting. `central_runtime`
connects the existing Control/gateway/evidence/result owners to short transactions.
Models, provider calls, evidence inspection and drain never hold a SQL transaction.

## Parallel execution and replanning

Worker, tool, target, model, action, token and time limits all apply. A profile's
role limits may reduce the effective worker bound. The report shows applied limits.
Notifications are wake-up hints; consumers also rescan authoritative state with a
bounded fallback. Replanning can consume intermediate evidence and result questions
while another worker continues. It does not wait for an entire batch to finish.

A Task owner writes with its monotonic ownership generation. Old owners cannot
commit after an atomic transfer. Resource conflicts use opaque execution keys
across workers/Runs. An approval/job wait releases worker/model capacity while
retaining the original action/job and resource obligation. Unknown calls retain
resources until actual settlement or explicit supported reconciliation.

Graph changes and repeated information do not define central progress or success.
Investigation can continue with unchanged graph content within actual budgets.
No separate question lifecycle, semantic duplicate-work blocker or investigation
ordering DAG is installed in the central runtime.

## Results, goal success and cleanup

Workers can publish intermediate results using `task.publish_result`. Questions
are result items with source Task/result/question identities. Registering a
follow-up does not resolve a question: its reviewed result and original evidence
must support resolution. Refuted or inconclusive investigation results are valid
outcomes and need not create a vulnerability Finding.

Task settlement and goal settlement are different. `central_completion` evaluates
the original Mission criteria and goal version. It checks criterion evidence,
required investigations/questions, relevant unresolved hypotheses, original
execution/approval state, graph integrity and required cleanup. An unrelated open
hypothesis or optional question may remain visible in a successful report.

The settlement gate stops new assignment/admission, then drains owned activity
within the configured timeout. A short final transaction rechecks relevant inputs.
A changed goal, relevant evidence/result or outstanding execution rejects an old
review; unrelated graph updates do not reject it. `goal_achieved`, `exhausted`,
`budget_exhausted` and `operator_stopped` remain distinct.

Providers declare resource effects through `redteam.resource_effects` metadata.
Version 2 includes the stable provider-owned backend/tenant `identity_domain`;
creation/removal tools for the same resource share it. Different domains with
the same local ID retain independent obligations. Version-1 originals are kept
but cannot prove domain identity or successful cleanup.
Actual output identifies confirmed creation/removal; the original result and
execution support graph cleanup records. Missing declarations are reported as
unmapped and prevent successful goal settlement; they are not guessed. A cleanup label without the original removal evidence
cannot satisfy the success gate.

Completion checks relevant evidence again after review and checks conditions,
registered binding references and observed expiry in the final central commit.
Unavailable optional results remain reportable without blocking an independently
proved criterion; selected criterion sources and essential investigation/required
question receipts retain their checks. Public and native unresolved Task APIs
use the same central result commit and preserve follow-up questions without
marking the Mission successful.

## Approval, stop and restart

Approval resolution checks the original Run, approval ID, action, tool, normalized
inputs and fingerprint. Resume continues that precise action; it does not ask a
worker to invent another call. Completed calls are not resubmitted. Original
external jobs are collected by their original job references.

Operator pause/stop closes new admission independently of a model response.
Cancellation drains within the configured timeout. A call with no trustworthy
terminal result remains unknown and retains its resources/accounting.

Restart checks original scope, component/profile identity, operative settings,
checkpoints, evidence, approvals and shared usage. A reserved, never-entered actor
may be recovered only with its original identity and absence of contradictory
activity. An entered actor with missing/incompatible SDK state is refused. Lease
expiry alone does not prove recovery is safe.

Resource waits continue automatically on the same returned actor when another
actor releases capacity/resources, without another proposal or Control charge.
The driver checks canonical ownership and original SDK calls, then reserves the
original worker slot. It does not transfer ownership or reload peer accounting.
UNKNOWN, active jobs, pending approvals and operator pause/stop remain distinct.
Identical non-retry-safe calls use an opaque physical invocation key even when
the provider has no resource binding. New Task/call IDs cannot bypass an
unresolved hold. Normal resolved reinvestigation and different explicit
account/target contexts remain possible; LOW risk does not imply retry safety.

`resume --instance ID` continues only the selected original. It does not launch
READY siblings, resume a planner or assess the whole goal. Completed selections
return from central records without opening their SDK. Interactive wait callbacks
keep that selection; full resume omits `--instance`. Run-common binding/scope and
usage checks remain required. Settings overrides preserve historical passive
identity, while changed live limits are still rejected. During resumed actor
activity, status reports `in_progress` and retains the prior settlement reason
in `previous_termination_reason`; reading status never changes Mission state.

## CLI and retained storage

`run` and `resume` drive central state by default. `status`, `findings`, `report`
and Run-scoped `approvals` query current central records; they do not treat an old
report export as state. Read-only inspection neither starts actors/providers nor
creates/upgrades a database. See the README for environment variables and commands.

Evidence/Artifact originals, Audit/Approval, SDK checkpoints, derived report files
and external VDB/MobSF state remain separate. Central rows reference and validate
these originals; they do not copy their bodies into another truth store.

Central state is required by application assembly and the SDK runner. The
legacy execution option, Path/Work scheduling and old cohort resume are removed.
Historical database formats are retained for explicit offline migration and
read-only inspection; they cannot select an alternative execution engine.
Actual request/action/time budgets and live concurrency limit execution.
Historical strategy-revision and cumulative activation counters do not stop work.

Legacy import is explicit and offline. It preserves original IDs, evidence,
approvals and accounting, validates manifests and retires source writers after a
committed import. Current preparation supports reserved zero-entry actors with no
execution rows. Entered legacy SDK states require an adapter and are refused.
Keep source databases and original stores for restoration; do not mix writers or
reset budgets. See [central state](../contracts/central-state.md) for exact conditions.

Central artifact/code-fixture execution helpers remain explicitly unsupported;
Artifact storage and its integrity guarantees are retained. The graph shares
central storage, but a new relation graph and unified context API are not implemented.
