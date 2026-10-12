# Central state foundation (schema 3)

The foundation implements plan1a, plan1b and the explicit plan1c migration
entrypoints. Plans2a–2c connect the planner, native workers and original
approval/job/accounting recovery. Plan2d connects criterion completion,
cleanup, reporting and CLI consumers, and makes central state the application
default with two workers. The existing Attack Graph shares this database;
Evidence, Audit, Approval and SDK checkpoints retain their own storage.
User data is never migrated automatically.

## Selection and lifecycle

`redteam.app.bootstrap.build_central_storage(engagement, run_id=..., path=...)`
returns `(state, network_ledger)`. The default path of this explicit factory is
`data/orchestration/state.sqlite3`; relative paths resolve against the caller's
current working directory, just like the existing storage paths. Development
and tests should supply a separate temporary database and a separate Run.

The factory initializes SQLite and supplies Control's recovery inventory check.
It constructs the existing `SQLiteNetworkGrantLedger` in the same file only when
an explicit `network_budget` is supplied. It does not enable the gated shell or
open transports. Reusing the ledger requires the original budget; setup never
resets its reservations or usage.

Create the existing `MissionPlan` with `state.create_mission`, then call
`state.bind_run(mission_id, scope=engagement.scope, binding_hash=...)`. The hash
must identify the actual original configuration/component selection. The
binding is immutable, and a Mission belongs to one Run. Changed bindings or
scopes require a new Run. A raw `CentralStateStore` must be set up before use;
when the file contains network accounting, recovery also requires the
Control-owned activity check supplied by the factory.

Connections belong to one worker thread and close after each operation. WAL
and the existing SQLite utility provide three bounded lock attempts, a
one-second busy timeout, rollback and cancellation-safe transaction completion.
The component-owned `state_schema` version does not consume the file-wide
`PRAGMA user_version`. Empty databases upgrade from version 0 to 3 under
`BEGIN IMMEDIATE`; concurrent initialization rechecks the version under the
lock. Future/invalid versions and existing noncentral database paths are
refused. Version 1 upgrades to 2 by adding the graph tables, preserving existing
central records. Version 2 upgrades to 3 by adding migration provenance and resume
refusal records. There is no automatic legacy data migration.

## Authoritative records

| Table | Meaning |
| --- | --- |
| `state_missions` | Existing Mission, criteria/evaluation references, revision/status/termination reason |
| `state_runs` | Run–Engagement–Mission identity, original scope and configuration binding |
| `state_tasks` | Existing MissionTask purpose, subjects/scope/evidence references, role/capability, status, owner/generation and investigation result/questions |
| `state_registrations` | Exact registration request ID → original Task/batch and immutable request hash |
| `state_executions` | Original action/invocation/model tool-call IDs, Task/instance/generation/goal revision, proposal/approval/reservation/result references, job wait and resource state |
| `state_execution_resources` | Indexed opaque resource keys pointing to execution rows; no separate lease or claim lifecycle |
| `state_evidence_refs` | Original external evidence ID and inspected record hash; no body copy |
| `state_intents` | Existing IntentRecord, including its single authoritative application outcome |
| `state_requests` | Existing RunRequestLedger policy, request and provider-cooldown records |
| `state_activations` | Original instance/Task/owner/generation, reserved/entered/returned proof and configuration binding |
| `state_outbox` | Existing envelope/sequence/lease/delivery contract |
| `state_migrations` | Committed import ID, original file manifest/hash and disposition report |
| `state_migration_records` | Original row key/hash, active/retained reason and target key/hash; no duplicate bodies |
| `state_migrated_runs` | Original recovery identity, accounting/checkpoint references and refusal reasons |
| `state_graph_nodes`, `state_graph_edges` | Existing Attack Graph rows with Engagement namespace and indexed type/endpoints |
| `state_graph_mutations` | Existing GraphMutationReceipt, keyed by namespace/Run/original mutation ID |
| `network_pools`, `network_windows`, `network_seq_log` | Control-owned quotas, reservations, settlements and original window/session/sequence IDs when selected |

SQL primary keys and indexed projections serve point/Run/status queries.
Domain JSON is the original record, rather than a second Runtime snapshot.
Point LLM request updates load only their referenced rows; accounting inventories
are limited to that Run. Task lookups do not load Task/Path/Work inventories.
There are no TaskClaim, independent question, Path/Step or candidate scheduler
tables. Legacy Task dependency/parent/priority fields are rejected on registration
and omitted from central persisted Task JSON.

The additive `MissionTask` fields default safely when reading older records.
`InvestigationResult` distinguishes confirmed/refuted/inconclusive/blocked from
Task progress and Mission success. Questions remain result items with origin
and optional goal-required reason. A new Task can explicitly name its source
Task/result/question; registration never promotes or resolves the question.
The original completion receipt is referenced rather than copied. The initial
foundation stored criterion `goal_revision` and termination reasons; the current
owned planner also evaluates and commits goal achievement through the criterion
and original-evidence checks described below.

## Atomic operations and external boundaries

`state.transaction(callback, write=True)` runs a **synchronous** callback with
`CentralTransaction`. It owns the connection, `BEGIN IMMEDIATE`, commit and
rollback. Participants use `tx.db` and must not commit, open another connection,
perform external I/O or publish events. Awaitable callbacks are rejected.
`tx.graph` joins Task result, graph rows and mutation receipt in this same
transaction. It also uses the outer connection and never commits independently.

Register using `state.register_task(request_id, mission_task, checked=...)`.
The same request/content returns the current original Task, even after its
owner/status changes. Reusing the request ID with changed content fails.
Different requests can investigate the same subjects and evidence. There is
no semantic fingerprint, information-subset check or graph-change gate.

`claim_task` atomically changes the Task owner and monotonically increases its
ownership generation, with the minimal reserved activation proof in the same
commit. Owner writes compare that generation. `update_task` additionally checks
only that Task's reviewed content hash, so unrelated siblings do not invalidate
it. Pending intents and inactive Missions reject new admission. Approval/job
waits and unknown executions prevent general reassignment. Releasing ownership
does not expire or refund uncertain activity. Recovery here is limited to an
original **never-entered** proof with no contradictory execution, LLM or network
activity. Entered/checkpoint recovery is implemented by the explicit plan2c
consumer described below, rather than inferred from elapsed leases.

`check_evidence` inspects immutable filesystem originals and their index outside
SQLite. Pass its `CheckedEvidence` results into the transaction; only references
and hashes are stored. Adding references and `finish_task` is atomic, including
questions and optional event/graph participants. Identical result replay has no
additional effect; changed results are rejected. A negative or inconclusive
result can finish a Task while its Mission remains active.

Execution bookkeeping reuses the original journal receipt validation. The
submission binds the original tool, arguments/target digest, Task purpose,
instance and model tool-call ID. Starting it rechecks owner, goal revision,
resource reservation and admission. Approval wait resolution requires the
original approval/action/fingerprint. A terminal result must match the original
receipt and must not be uncertain. Running cancellation without an original
result remains unknown, including for tools with no shared resources. Unknown
executions cannot rewind to running. Neither cancellation, a terminal label nor
an expired lease releases the `resource_execution_pending` conflict. Known
settlement clears it; external job waits retain it. These operations grant no
Control permission and make no exactly-once external execution claim.
`execution_resource_keys(tool, invocation, principal_ref)` combines existing
namespace/value resource hashes with an exact invocation hash for tools that
do not explicitly declare `retry_safe=true`. This hash includes the Run-bound
provider/tool, normalized input and approved subject digest, and registered
principal context. Session/account arguments participate in that digest;
actor, Task, action and invocation IDs do not. Hidden provider sessions cannot
be proved independent by inventing new IDs. No raw arguments or credentials
are stored as resource keys. Atomic reservation and execution entry both check
the existing resource index; an unresolved UNKNOWN or job retains its hold
regardless of lease expiry. Approval-only intents do not hold the derived exact
key before approval; declared physical resource contracts still apply.
Resolved calls permit a new investigation. Explicit retry-safe tools still obey
declared resources and all capacity/admission limits. Existing rows without an
exact key are preserved: a matching unresolved tool/input is conservatively
held because the old principal context has no independent proof. No historical
execution identity is rewritten.
Pending historical intents derive transient comparisons from their verified
original receipt and current bound descriptor at readiness, reservation and
provider entry, preserving their persisted resource keys and identity hash.
Declared physical resource conflicts apply across Runs; the exact invocation
key is scoped to one Run. These projections
join authoritative execution rows rather than a second lock/duplicate database.
Resolving one approval does not clear another invocation's wait. Late original
results can settle retained executions without undoing Task cancellation.

The driver rechecks returned actors with pending resource intents on completion
notifications and bounded canonical scans while actors are running. When free,
it reserves the original actor slot under owner/version and worker/tool/target
bounds and continues its original SDK calls. It preserves the Task, actor,
action, invocation, model call, approval and charged Control admission. This
live continuation does not rehydrate peer usage or transfer ownership. UNKNOWN,
jobs and unresolved approvals remain held. Pause, stop, pending operator changes
and settlement reject entry; duplicate/lost notifications cannot authorize it.
An unverified original checkpoint is retained for review, not replaced. After
quiescence or restart, explicit resume uses the existing recovery contract.

`resume --instance ID` validates and resumes only that original actor. Terminal
selections return from current central records without opening an SDK thread;
foreign checkpoints/LLM owners, READY siblings and whole-goal settlement are not
driven. Interactive approval/job watching retains the same selection. Shared
Run scope, binding and usage integrity checks still apply. Omitting `--instance`
performs full recovery and can continue planning. Settings overrides use
`OrchestrationSettings.with_overrides`, preserving passive historical identity
through validation and assembly while rejecting changed operative bindings.
Read-only status projects active activation rows as `in_progress` and exposes
an old settlement reason separately as `previous_termination_reason`. It never
clears or rewrites the historical Mission reason merely to display live work.

`request_ledger()` reuses the existing LLM accounting implementation with the
central transaction boundary. Unknown dispatches stay charged across restart;
only undispatched reservations can be released, and settlement/release replay
cannot refund twice. `apply_intent` commits the supplied validated domain effect,
authoritative IntentRecord and queued envelopes together; replay does not repeat
the effect. The legacy runtime keeps its existing intent consumer. The central
development selection applies pause/stop/exclude through this transaction before
new calls. `deliver_outbox`
reuses the existing dispatcher, with leases committed before publishing and
acknowledgements afterward. Delivery remains at least once.

`inspect_evidence` reports file/index disagreement, missing or changed originals,
and external-only records after lost acknowledgement. These are diagnostics,
not automatic repair or global exploration fences. SQLite cannot atomically
commit a filesystem append; callers must preserve the original before linking,
recheck integrity when consuming it, and reconcile affected IDs explicitly.
Unrelated valid evidence remains usable.

## Central graph selection and shared commit

`build_central_graph(state)` returns the existing Engagement namespace wrapper
around `CentralAttackGraphStore`. Initialize and bind `state` first. Pass this
store to `AttackGraphService` or existing graph consumers; the factory does not
wrap it in `PlanningGraphStore`, create a planning writer, or select a runtime.
Its graph knowledge remains shared across Runs in one Engagement. Original Run
provenance stays in claims and mutation receipts; receipts from another Run are
not exposed through this Run's selection.

Point, type/label/attribute, neighbor, incident-edge and bounded neighborhood
queries read SQL directly with namespace/type/endpoint indexes. The service,
view and namespace wrapper forward `subgraph_around`; `ContextProjector` uses it
without enumerating the graph. This backend declares `cacheable=False`, so that
consumer disables its optional caller-revision cache. Every subsequent lookup
sees a new SQLite snapshot, including other processes' committed changes.

The optional `AtomicAttackGraphStore.mutate_node/mutate_edge` contract runs a
pure synchronous callback against the latest row under `BEGIN IMMEDIATE`.
Services reuse their existing merge/retraction rules in that boundary rather
than reading a value before the lock and overwriting a newer merge. Node and
edge first creation, properties, original timestamps and claim provenance are
protected by the same transaction. Legacy backends keep in-process fallback
serialization. Row callbacks must not await, do external I/O or change identity.
Central raw `add_*` calls merge incoming observations; explicit snapshot restore
uses row updates to preserve the existing replace-versus-merge API.

Prepare the original `GraphMutation` through the namespace wrapper **before**
opening a shared transaction. Preserve its exact ID and payload for replay:

```python
graph = build_central_graph(state)
prepared = await graph.prepare_graph_mutation(original_mutation)
checked = await check_evidence(evidence, run_id=state.run_id,
                               engagement_id=state.engagement_id,
                               evidence_ids=result.evidence_ids)

def settle(tx):
    tx.finish_task(task_id, result, owner=owner, version=generation, checked=checked)
    receipt = tx.graph.apply_graph_mutation(prepared)
    tx.queue(result_event)
    return receipt

receipt = await state.transaction(settle)
await state.deliver_outbox(event_bus)
```

Inspect all result/question/completion evidence IDs before this transaction.
No async store or service call belongs inside `settle`. Graph additions are
incoming assertions, not premerged snapshots; their merge is evaluated against
the latest row. Same mutation ID/content returns the original receipt without
replaying a change, even if later writes have changed the graph. Changed content
with that ID is rejected. Any graph/receipt/reference/result/outbox failure rolls
back the whole operation. External file writes and event delivery remain outside
that commit; receipts prove storage only.

Whole-Engagement reads remain intentional for `list_nodes/list_edges`, full
`observe/resolve`, integrity checks, snapshot export, cleanup/retraction sweeps,
global traversal/evidence chains and rendering without a focus. The existing
knowledge collector reads all nodes and relation summaries for aggregate
assessment/reporting. `HypothesisService.relations_for` now uses incident edges
for one hypothesis. These inventories are SQL reads without a persistent memory
mirror; this stage does not introduce a combined context API.

The plan1c adapter retains the existing snapshot schema-1 `graph_meta/nodes/edges` format
from `to_records/from_records`; importing through the correct Engagement wrapper
restores the existing physical namespace IDs and original timestamps/provenance.
Legacy `graph_mutation_receipts` already use the existing receipt model. Their
original namespace/Run/mutation ID/hash/references must be migrated unchanged,
with corresponding Run bindings; do not issue new receipts for historical
changes. Import does not replay mutations or merge away historical provenance.

## Explicit offline migration (adapter version 1)

`redteam.app.legacy_migration.migrate_legacy_storage(request, ...)`
accepts a `MigrationRequest`: migration ID, separate target path, explicit source
paths, original repository references and `offline=True`. Source owners are
`task` (required), `work`, `graph`, `intent`, `mission`, `checkpoint`, and `network`.
Omitted databases are not created. Relative paths resolve against the caller's
working directory. Evidence/Approval/Audit ports must read the same original
repositories named in `references`; artifact paths/hashes remain external.

All related source writers and network controllers must be stopped. This cannot
be inferred from stable hashes or a free Run lock. The tool copies SQLite together
with WAL/SHM, checks original hashes before/after capture and import, and reads only
query-only disposable copies. It never opens or sets up an original database.
The report includes actual source table counts and every original row's disposition.

Supported contracts are orchestration schema 1, MissionPlan and MissionTask v1/v2,
activation launch proof 1, native execution origin 1 with original journal receipts,
graph snapshot/receipt schema 1, LLM policy 1 and the exact current Control network
schema. `PRAGMA user_version=0` alone proves none of these. Missing identity,
unreadable records, index disagreement and unsupported formats are retained and
refuse affected Run resume. Question Tasks, negative results, follow-up source
triples, graph provenance and retractions keep their meaning. V1 alone is not a
reason to discard an investigation Task.
Legacy logical edge keys are physically namespaced only when both original
endpoints prove the same engagement. Their logical ID, source row/hash and
immutable mutation receipt/hash remain unchanged; uncertain tenancy is refused.

`inputs_by_run` must contain the original `specialist_planning_inputs`, whose
schema-2 hash matches the saved engagement/scope/policy/component binding. Missing
bindings are not synthesized from current configuration. Unverified Runs remain
recovery inventory without fabricated active Run/Task/instance/action records.
Source ownership version zero maps to central generation one only with an original
unique instance and validated launch proof; the original mapping remains recorded.

Work candidates/priorities/DAGs are not imported. Actual executions retain original
action/invocation/model-call IDs, proposal/result evidence IDs, approval/fingerprint,
job and resource state. Control's `inspect_execution_approval` checks its own audit
and approval contracts without granting permission. When a legacy receipt lacks
the goal version and the current goal revision exceeds one, `goal_revision=None`
preserves uncertainty and refuses resume. The latest version is never substituted.
Unknown executions and unconverted resource markers keep their shared conflicts.

Coordinator intents retain their authoritative received/applied/rejected status and
outcome; matching or delayed history copies remain projections. Import applies no
intent and publishes no event. Outbox delivery flags, sequence floors and leases
survive. All LLM states, costs, original policy and cooldown are preserved. Control
validates complete network pools/windows/sequence accounting, including other
sharing Runs and quarantined reservations. Column values are copied exactly,
without setup resets, releases or refunds.

Path/Step, legacy tasks/links/transitions, unused `mission.sqlite3` revisions,
planning writers/references and validity/Runtime copies remain in original files
with row hashes and reasons. They are not central planning prerequisites. Pending
planning writes refuse the Run and are retained; they are not silently cleared.
Immutable receipt bodies, digests and approval proposals are never rewritten.

The import and target row/hash reconciliation commit in one SQLite transaction,
without filesystem/provider I/O inside the lock. The same migration ID/source
manifest returns the committed report without replay. Changed sources or another
ID for the same Run fail. Transaction failure rolls back the entire import.

After commit, source hashes are rechecked and **explicit migration** creates private
`.DB_NAME.central-migration.json` companion files beside the selected sources.
They fix one canonical destination, migration ID and source hash; SQLite/WAL/SHM
contents remain unchanged. Existing coordinator/Task/graph/network writers refuse
retired paths, including new legacy Runs. Another target cannot obtain a fresh
copy of the budget. A crash before every fence is finalized refuses target resume;
an exact retry finishes fences without importing twice. Do not remove fences to
reuse old ledgers. Original Work/checkpoint paths also remain frozen archives.

`validate_migrated_run(state, current_inputs=..., current_policy=...,
component_check=..., evidence_store=..., approval_store=..., audit_store=...)`
checks original source/fence hashes, current bindings, immutable evidence and
Control approval links. `component_check` must validate the assembler's selected
registry/binding without starting providers. This check grants no execution rights.

`migrated_run_guard(state, **validation)` surrounds the entire future execution.
It holds the original cohort's canonical Run ownership inodes, rechecks validation
after acquisition and revokes the context permit on exit. Central admission refuses
migrated Runs without this guard or with recorded refusal reasons. A new path
does not acquire independent ownership of an original Run or shared quota.

Checkpoint support is constructor-free inspection of each original thread's latest
root checkpoint: `msgpack`, SDK checkpoint version 1/2 and current Runtime schema
and bindings. Files/hashes remain external; no older fallback, typed restore or new
thread/Runtime is created. Entered instances require matching checkpoints. A
pre-submission approval whose proposal exists only in its original checkpoint keeps
its action/approval IDs, but refuses resume with
`checkpoint_approval_requires_runtime_adapter`; no journal receipt is invented.
Closed Runs, missing owner/proof and unsupported checkpoints are also refused.
Actual typed checkpoint/approval/job consumers belong to plan2.

The inspected local dataset differs from supported current code: all 76 Tasks are
v1 without owners/results; all 67 Work rows are origin 0 without instances, and
the Work table lacks `instance_id`. Graph storage has 161 nodes, 65 edges and a
schema-1 header, without mutation receipts. The 34 orchestration records contain
binding/admission/outbox/counters, without native activations or LLM accounting.
Unused `mission.sqlite3` has seven revisions/seven bindings; intents are empty and
network storage is absent. Checkpoints have 2,903 `msgpack` checkpoints/33,616 writes,
which does not establish current Runtime compatibility. These observations do not
authorize migration or resume of user originals.

## Plan2a/Plan2b development selection

`redteam.app.central_development.build_central_development(engagement,
llm=..., tools=..., settings=..., jobs=None)` is an async context manager.
It requires an explicitly selected existing specialist profile and LLM request
policy. The accepted providers are exactly `FakeLLMClient`,
`DevelopmentToolGateway`, and optionally `DevelopmentJobs`. They accept scripted
data, with no transport, shell, model service or arbitrary execution callback.
Other providers and unwired code-fixture selection are refused before creating
storage. This is an explicit fixture assembly, never a production fallback.

Every selection creates new Engagement/Run identities and owns a temporary
directory containing `state.sqlite3`, `checkpoints.sqlite3`, immutable Evidence,
Audit and Approval stores. Those files exist only inside the context. The API
does not accept an old Run, an arbitrary DB path or an external capability
factory. The existing component factories, selected role/profile registration,
role tool manifests and local tool registry are reused. The binding includes
the profile/tool fingerprints and the `central-development` version marker;
the ordinary runtime cannot silently interpret this binding as its own.

`CentralDevelopment.run()` uses `SpecialistPlanner`, the existing native
`MODEL → TOOLS → SETTLE` loop and `InstanceDriver`. Plan2a established a single
worker; Plan2b connects parallel ready Tasks on the same driver.
`central_runtime.ready_workers` atomically claims ready Tasks and records
the original instance/owner/generation. The runner checks that minimal launch
proof, the original configuration, full Task completion contract and original
run clock against central records before entering. Task lifecycle has one
authoritative row; checkpoint counters/messages are execution continuation data.

`register_investigation_tasks` validates purpose, evidence, known subjects,
capability and completion conditions. It uses `tx.register_batch`: the entire
request content is hashed, including all child keys. Replaying the same request
returns its current original Tasks; changing any content under that ID fails.
A new request can intentionally investigate the same subjects/evidence again.
There is no Task dependency DAG, active Path requirement, operation-attempt
filter, graph-digest admission or graph-change/no-progress stop. The selected
path never reads or writes legacy Task/Path/Work domain stores. Real input,
role, scope, ownership, approval, time, action and LLM limits still apply.

External calls retain the existing action/invocation/model tool-call IDs.
An immutable original submission is preserved and its central execution row
committed before Gateway entry. The ordinary Control, Gateway, verify and update
flow is reused; the Gateway is the explicitly selected local double. Original
result/evaluation evidence is saved and inspected outside SQL. Result
interpretation uses a disposable touched-row graph write set, then commits
execution state, inspected evidence references, graph changes/mutation receipts
and outbox events in one central transaction. Only afterward are notifications
delivered. A failed SQL commit leaves an external original for reconciliation;
there is no assumed filesystem/SQLite atomicity or claim of exactly-once calls.
Touched-row checks do not invalidate work because an unrelated graph row changed.
Concurrent updates to a shared row apply the existing synchronous row-update
callbacks to the latest SQL value, preserving independent observations. Deletion
and changed applicability remain fenced. The callbacks contain no external I/O;
interpretation and original evidence writes remain outside SQL.

The selected local tool `task.publish_result` publishes intermediate result
items while the Task remains running. It is registered through the existing
local tool registry. Result and question IDs remain stable through final
settlement; prior questions are retained unchanged. Follow-up Tasks cite the
original Task/result/question triple. `InvestigationResultUpdated` and graph
events wake the planner through a coalesced in-process notification; each
judgment reads current central originals. A finished Task also triggers another
judgment without requiring a graph change. Planning turns remain bounded by
the existing specialist limit and shared LLM policy.

Planner/worker/model repair/evaluation requests all use the same central
`RunRequestLedger`. An assembly-owned semaphore serializes requests under its
actual concurrency limit before reservation. Moving to another Task never
resets action/time/request accounting. `CentralDevelopment.status()` reads
central Mission, Task/result/question, execution and LLM accounting records and
identifies the selection as a development double.

Approval/resource waits preserve the original pending action. Jobs retain their
original job/invocation IDs. Uncertain executions remain unknown; physical
resource conflicts and usage are retained while the worker actor returns its
slot. They cannot be reassigned or automatically retried. In this stage
`runner.resume_recovered` only continues the latest matching original checkpoint
when every prior execution is known and settled and the Task remains running.
Completed calls restore their original references without Control/Gateway replay.
Approval/job/unknown recovery, checkpoint rewinds and legacy recovery entrypoints
are explicitly unavailable on this selection.

Negative/inconclusive/blocked investigation results can finish a Task without
asserting Mission success. Positive Task completion still uses the existing
origin, criteria, evidence and semantic review; development-double outputs cannot
prove a Finding or positive completion. The selected `goal.settle` reports that
goal assessment is not wired, and never sets `goal_achieved`. A held worker or
empty ready set leaves the Mission active.

## Plan2b parallel lifecycle

`InstanceDriver.drive_ready_tasks` replenishes available slots from central Tasks.
Each actor remains an independent asyncio task and LangGraph thread. The driver's
first-completed delivery returns an actor slot before admitting another Task;
it does not wait for siblings. The central Mission/Task/execution rows remain
authoritative. `CentralDevelopment.instance_results` holds per-instance returned
reports; the planner's Runtime is never replaced by a worker's Runtime.

The planner has a separate bounded asyncio task and the same request meter as
workers. `EventWakeup` coalesces committed evidence, graph, result, actor-return
and operator notifications. The existing outbox's committed sequence supplies a
read cursor; duplicate delivery or an unchanged periodic scan does not request
another judgment. This is not a graph fingerprint or an investigation admission
filter. During activity, bounded `resource_wait_timeout_seconds` deadlines reread
canonical Tasks/controls and detect undelivered commits. A new driver invocation
also scans the DB before starting; quiescence is held, never goal success.

| Bound | Original configuration and retained state |
| --- | --- |
| Active workers | Minimum of specialist `max_instances` and `global_concurrency`; reserved/entered activations count until actor return |
| Tool entry | Minimum of `global_concurrency`, Engagement `max_concurrent_actions`, and configured ROE parallel bounds; existing tool semaphore plus original central execution reservations/activity |
| Target entry | `per_target_concurrency` against receipt-verified canonical target identities |
| Model requests | Shared central `RunRequestLedger`, configured total request budget and `max_concurrent_requests` semaphore |

`TaskExecution.target_node_id` and `target_identity_verified` are immutable
submission-derived capacity projections. Verified target-free calls have no
target slot; historical executions without verified identities conservatively
occupy target capacity. Existing historical execution identity hashes are
preserved when these optional fields were absent. No extra table or queue is
created. Context/status budget display reads original Control decisions from the
central outbox; only Control grants execution capacity.

Policy admission/charging and durable submission reservation use the existing
shared policy lock. Immediate Control re-admission and SQL execution entry use
that same lock, closing the two-workers/last-slot race. Provider invocation is
outside it. SQL capacity checks also protect original pending reservations;
target/resource capacity waits retain the precise unstarted submission and
approval/budget references. They are held for original-action recovery, not
placed in another work queue.

Approval/job/resource waits return actor and model slots without clearing Task
ownership or execution records. Running calls, external jobs and unknown calls
keep tool/target capacity after actor return and resource lease expiry. Approval
waits consume no external-call slot. Failures do not stop independent siblings;
unsettled originals remain blocked, with a `needs_review` marker when appropriate.
The activation stores only original identity, owner/version, binding and launch
stage. Task completion itself does not declare the actor returned.

Original-call continuation still verifies the latest matching checkpoint,
ownership/binding and all known settled executions before re-entering the same
actor. The plan2c section below adds explicit approval/job/unknown collection
and verified owner transfer.
There is no automatic handoff or repeated invocation after lease expiry.

The plan2b development binding was `central-development:2`; plan2c advances
it to `central-development:3` for its recovery and result-mutation semantics. The fixture
providers offer per-Task scripts and event barriers for independent actors,
without external providers or arbitrary execution callbacks. Tests cover worker
counts one/two, simultaneous gateway entry, independent failure, two approval/job
waits plus a third Task, retained capacity, shared budgets, stale ownership,
idempotent result delivery, intermediate follow-ups, deterministic concurrent
graph updates and undelivered/reopened-DB scans.

## Plan2c: approval, accounting, cancellation and explicit recovery

`app.central_run.CentralRun` owns the existing planner/InstanceDriver lifecycle.
The development builder uses that same class with exact local doubles; there
is no second scheduler. `open_central_run(config, directory=..., settings=...)`
selects the existing application component, role, LLM, MCP and Control assembly
explicitly. A fresh selection requires a separate empty directory. Domain
state and graph use `state.sqlite3`; SDK checkpoints, Evidence, Audit and
Approval remain separate. No legacy task/path/work/network DB is built for
this selection. (Plan2e subsequently removed the unused in-memory compatibility
containers as well.) The initial in-memory defaults created no durable
state or queue and must not be used to drive this Run.

The selected directory fixes all original storage locations. Resume requires
`resume=True`, the original `run_id` and `engagement_id`, and existing central
state/checkpoints. It validates scope, actual component/role-tool/prompt
bindings, LLM selection, request policy, execution limits and selected gated
network/shell policy. It does not replace a missing Run or reinterpret a
legacy checkpoint. Catalog discovery can open configured MCP transports; it does not run model
completions or target tools. Plan2c retained the original defaults; plan2d below
supersedes that selection after focused safety verification.
The legacy `run_engagement`/`settle_engagement` entry points reject central
selection; use the selected session's `run()` and original recovery API.

Approval identity remains on **each** actor's `pending_approval_id` and
`pending_action_id`; these fields were retained. Every central `TaskExecution`
links the exact action/invocation/model-tool-call, immutable submission evidence,
original instance/owner epoch, approval ID/fingerprint and job reference. The
actual approval/resolution and decision audit remain Control-owned files.
Multiple waits are queried from executions, never a global approval slot.
Approval/resource/job waits block that Task's ordinary assignment. Returning
an actor slot does not refund its admission or cancel its external job.

Normal entry order is: validate current ownership/operator scope, serialize
Control evaluation/charging under `policy_lock`, persist original audit/events
and submission/SQL reservation, release the lock, then revalidate at provider
entry. Current Control admission and the SQL RUNNING intent commit share the
same lock. SQLite transactions never await LLM, transport, human or job I/O.
Original result files precede interpretation; execution settlement, prepared
graph changes and outbox events commit together. The processed-evidence seal
and notification follow. Verified resolution of an uncertain envelope uses
its original receipt ID in graph mutation identity, so the same invocation's
later result cannot conflict with the earlier uncertain interpretation.

`DurableControlRecovery` exposes inspection and restoration, not execution
permission. Control restores unique admitted actions and original extra-call
reservation keys from audit, monotonically retaining consumed budget and the
original duration clock. The current rate window is conservatively restored.
An already-paid pending intent reuses its exact original decision; approval
resolution charges once. Missing/mismatched original admission is held.
The shared request ledger retains reserved/dispatched/settled/unknown/released
rows and original IDs. Under exclusive quiescent Run ownership, a lost reserved
request is released; a lost dispatched request becomes charged unknown.
Actor handoff never issues a new request allowance.

The existing gated provider assembly injects `SQLiteNetworkGrantLedger` at
**the central database path**. Pool/window/sequence data and unknown quarantine
are retained on rebuild; there is no second network ledger. Runs without gated
selection do not gain a new network policy. Pending external executions/jobs
continue to occupy tool/target capacity and shared resource keys after lease
expiry. Only proved non-entry or a verified terminal original can release
external reservations.

Explicit original recovery is `session.recover(instance_id, transfer_owner=...)`
or `InstanceDriver.recover_central_instance(original_runtime, ...)`; follow a
ready outcome with the existing runner/driver resume API. Its order is:

1. Own the original Run exclusively and require quiescent actors.
2. Verify the latest SDK runtime, original thread, Task owner/version, original
   Run clock and selected bindings. Never replay an older SDK branch.
3. Restore request/Control accounting; inspect each required evidence ID/hash
   and file/index integrity before any SQL lock. A checkpoint call without an
   original execution intent remains held.
4. Query/fetch the **original** job or use the existing execution recovery
   reference/provider. This never invokes or resubmits a tool. Known committed
   results are inspected and reused; missing/uncertain originals remain held.
5. If requested, CAS the current Task owner and increment its version, keeping
   the same original actor/thread/action/approval/job/result IDs. Runtime
   `task_owner` carries that recovered owner epoch. Execution ownership versions
   continue to identify their original admission, while settlement checks the
   current Task owner/version separately. Competing transfers of one expected
   epoch succeed once; stale owners cannot enter calls or complete the Task.
6. Update the original SDK thread with the recovered owner epoch, reserve its
   actor slot and continue the original native calls. Approval resumes check the
   exact stored proposal before Control; deny reasons become tool feedback.
   Completed calls restore references without execution or charging.

SQL, SDK and external files are not globally atomic. A crash between owner CAS
and checkpoint update leaves an explicit binding mismatch and refuses resume.
It never invents a new actor or budget. Handoff stays held for unverified external execution or an unresolved network
window. A job proved pending/running through its original backend can transfer
the Task owner while keeping the same job and full reservations; the actor
stays held until its original result is terminal. Unknown job status refuses
transfer. Original status/result collection may continue. An entered actor with no readable checkpoint can only collect
originals via `collect_checkpointless_originals`, without interpretation,
Task completion, owner release or replay. A never-entered reservation can reuse
its original actor only after original proof, SDK absence and **no** execution,
request/evidence/network activity are checked. Unsupported migrated original
proof/binding/checkpoint versions are refused rather than treated as empty state.

A separate owned control monitor applies pause/stop even while the first
planner response is blocked. SQL admission also rejects queued controls before
provider entry; current exclusions are checked again for each original action.
Async actor cancellation preserves RUNNING calls as UNKNOWN. Job cancel requests
are bounded and acceptance is not proof of terminal cancellation. Driver close
records returned actors; if a provider cannot drain within the configured bound,
it preserves uncertain records and retains Run ownership, raising a diagnostic
instead of reopening checkpoints while an actor can still write. Domain records
cannot guarantee exactly-once external side effects or external cancellation.

Safety checks use temporary SQLite, real Control/filesystem originals and explicit
LLM/tool/approval/job doubles. They cover independent approvals/deny/resume,
owner CAS competition/stale writes, shared budget and unknown requests, original
job completion without resubmission, central network quota rebuild, blocked
planner/parallel cancellation, binding changes, exact proposal and evidence
integrity refusal, never-entered proof, plus the preceding parallel regressions.
No paid model, live MCP/target, user DB migration or default switch is performed.

## Remaining integration

Plan2d supplies final goal assessment, reporting/cleanup, CLI integration and the
validated default switch. Code-fixture/artifact helpers are not registered on
the selected central lifecycle, and explicit code-fixture/artifact injection is
refused rather than routed through legacy scheduling. Plan2e removes consumers
whose use has ended. Historical schema and activation readers remain solely
for explicit offline migration.

The offline migration composition entry point is now in `app.legacy_migration`.
It injects the Control-owned `ControlSnapshotInspection` port into the unchanged
import algorithm. Approval inspection and network schema/accounting validation
remain in Control; orchestration no longer imports those concrete implementations.
No source database was migrated as part of this change.

## Plan2d: completion, cleanup, reports and default CLI

The application and CLI default to `OrchestrationConfig.runtime_backend=central`
and `max_workers=2`. `build_application` assembles the central state and durable
checkpoint path when no state is supplied. `run --workers`, `--state-db` and
`--runtime` override validated selection. Environment equivalents are
`REDTEAM_MAX_WORKERS`, `REDTEAM_STATE_DB`, and `REDTEAM_RUNTIME_BACKEND`.
The operator must select `REDTEAM_SPECIALIST_PROFILE` and a shared request
policy in `REDTEAM_LLM_REQUEST_POLICY_JSON`; no missing profile, policy, provider
or tool catalog is replaced by a test double. Actual worker, global call,
per-target, resource and Control limits still apply.

Schema remains version 3. Original settings/store references, the temporary
settlement admission gate and a planner exhaustion proposal use existing
Run metadata rows in `state_requests`. The borrowed LLM ledger view selects
only its own request/policy/cooldown namespace and retains strict row checking.
Mission `criterion_results` store original goal revision, purpose hash, actual
Task/evidence IDs and reviewed claims. Task results, questions, owners,
executions and the existing semantic graph remain the canonical domain state.
No Goal/Criterion/OpenQuestion/cleanup database or relation layer is added.

`CentralRun.settle` closes new assignment/provider admission, performs a
bounded drain on the owned driver, and evaluates the original Mission criteria.
The LLM review and evidence file inspection occur outside SQL locks. The final
short transaction rechecks the goal revision, investigation results/questions,
original executions and relevant hypothesis/cleanup inputs. Unrelated graph
observations do not invalidate the assessment. Incomplete/blocked required
investigations, necessary unresolved questions, unsafe executions, active actors,
missing originals/criteria or unverified required cleanup refuse success.
Inconclusive/negative investigations can supply useful original observations;
every Task does not have to establish a vulnerability. Confirmed Findings keep
the existing origin, target, claim and review guards; mock/development output
cannot prove a Finding or goal.

Registering a follow-up does not resolve its source question. A completed,
reviewed confirmed/refuted follow-up must cite the original Task/result/question
triple and supply a valid receipt and source evidence. Optional questions and
unrelated open hypotheses can remain and are reported. `goal_achieved` requires
criterion proof. `exhausted` requires a reasoned planner proposal bound to fresh
investigation inputs; graph fingerprints, repeated delivery or an empty draft
are not exhaustion. Actual request/action/time caps, operator stop/pause and
held/planning failures retain distinct reasons and original usage.

Providers declare `redteam.resource_effects` in MCP discovery metadata. Each
version-2 effect describes `created`/`cleaned`, resource type, provider-owned
`identity_domain`, output identity path and a boolean confirmation path.
The domain is the stable backend/tenant namespace shared by creation and
removal tools, even when their ToolIds differ. A provider spanning multiple
domains must return a qualified resource identity; a tool name is not a domain.
The generic MCP adapter validates and carries this
declaration with the preserved result. Only actual confirmed output registers
the existing resource-node cleanup property. Identifiers are hashed with the
Run/domain/type and typed identity value; the graph stores the domain and
creation action/evidence and cleanup evidence, not the raw resource identity.
Cleanup requires terminal successful output and the same resource. A removal
started before the current confirmed creation cannot close its newer obligation.
A new confirmed creation reopens the obligation; replaying the
same creation does not. Settlement verifies both retained execution originals,
so a `mark_cleaned` label alone cannot prove cleanup.

Version-1 effects remain decodable without changing their historical serialized
shape, but their identity domain is unproven. They cannot register/close a new
authoritative cleanup obligation or prove goal success. Old retained results
with these effects are reported as unmapped; old nodes and originals are not
rewritten. Previously collapsed obligations cannot be recovered by renaming a
node. Providers must supply version-2 declarations and a compatible Run binding;
this change does not migrate user databases or replay a target call.

Task and goal reviews capture only relevant original/binding hashes and measured
condition values/lineage. They refresh those reads after the model response and
check current graph conditions, registered binding references and observed
deadlines inside the final state transaction. Binding registration uses a
reference/hash entry in the existing `state_requests` table before evidence I/O,
so a new or missing acknowledged binding cannot slip through that final check.
Original file reads and model calls remain outside SQL. Unusable optional Task
support is excluded from the goal review instead of becoming a global success
barrier; actual criterion sources and essential Task/required-question receipts
remain checked. Independent optional updates do not invalidate the goal CAS.
Public and native unresolved Task settlement share the central result commit,
preserve questions/originals and grant no positive completion receipt.

Providers without structured resource effects (including arbitrary shell
output and existing adapters that do not declare effects) are listed as
`cleanup_unmapped_tools`. Their resources are not guessed, and a lack of
recognized writes is not presented as proof that they created nothing. A known
pending/failed obligation always prevents `goal_achieved`. Code-fixture/artifact
helpers remain explicitly unsupported on this lifecycle; they are not silently
routed into legacy execution.

`status`, `findings`, and `report generate` project current central state with
read-only SQLite and an integrity-inspected Evidence snapshot. No provider,
checkpoint replay or state migration runs during a read. The report includes
criterion evidence, investigations/owners, result questions, every original
approval/job/unknown execution, ledger usage, applied limits and cleanup
obligations. Exports are derived files, never recovery truth. An old success
flag is rechecked against current originals before release. `approvals --run`
uses original Audit/Approval references and central execution IDs; resolving an
approval never executes an action. Live CLI watching retains the same driver
and reconciles original instances without resubmitting their jobs/actions.

`resume` uses saved Run/store/settings identities. Changed scope, provider/
role binding, request policy, limits or missing entered checkpoints refuse
recovery; no replacement Run/instance/budget is minted. The existing original
clock and Control/LLM/network records are rehydrated. A missing SDK snapshot is
permitted only for a coherent versioned zero-entry reservation, rechecked by
the owned driver before execution.

Legacy import stays explicit and offline through plan1c's
`app.legacy_migration.migrate_legacy_storage`. For a validated Run that has only
version-1 **reserved, never-entered** activations and no execution rows,
assemble its original configuration and storage references with a separate
native checkpoint destination, then call `prepare_migrated_runtime(deps)`.
This atomically maps the central binding/launch proofs and records configuration
without changing the Mission, Task/instance IDs or usage. `resume_central_engagement`
holds `migrated_runtime_guard` for the entire execution and revalidates original
source fences, component/input/policy hashes, evidence and Control records.
Entered legacy SDK/execution formats require a verified adapter and are refused
with `requires_runtime_adapter`; this adapter does not convert them. Missing
original configuration must be prepared explicitly, never inferred by CLI.

No user database was imported or restored during verification. Preserve the
offline sources, migration manifest/fences and all separate originals. An old
backup can be restored only while every writer/cohort is stopped, after reverting
the whole compatible storage cohort to its original snapshot; removing a fence
from an already-continued central Run is not rollback. Do not dual-write or resume
a central Run through `--runtime legacy`. The focused tests use temporary DBs,
fake LLMs and explicit provider/transport doubles; no paid model or target call
is validation evidence.

## Plan2e cleanup and compatibility boundaries

Central assembly now constructs no empty legacy Task/Path/Work containers and
rejects their explicit injection. `RuntimeDeps.investigations` is the shared
read boundary; CLI/runner readers use it. Operator instructions select the central
owner directly. Goal/task review no longer reads or normalizes a path as part of
the central decision pipeline. Missing central wiring cannot be hidden by an
empty compatibility store.

The SDK graph registers only `model`, `tools` and `settle`. Legacy
startup/policy/worker/aggregate recovery nodes and their backend are removed.
A checkpoint carrying a legacy action continuation is refused. Central model
context also omits empty legacy path/delegation display fields.

New central configuration records omit separate Task/Work/path/graph/intent DB
paths and backend selectors. Durable CLI setup selects SDK checkpoint persistence
without activating those domain stores. Original central configuration records
remain readable: comparison ignores only the retired backend/path fields and
still checks operative settings, applied limits and all actual original-store
references. The old five ranking/pruning knobs have no runtime consumer and are
removed from the settings schema. Their historical defaults remain solely in the
binding identity payload; changing an old nondefault identity still fails closed.

The unused graph ancestor/descendant/shortest-path, rendered evidence-chain,
HTML/SVG and public snapshot import/export APIs were removed from the service,
view and package exports. Core's graph store Protocol never required them.
`subgraph_around`, provenance/confidence/retraction, cleanup, integrity and atomic
graph mutation receipts remain. `snapshot.py` is an internal SQLite/migration
codec, with version/model/sensitive-property validation. Test storage fixtures
are explicit and never registered by application assembly.

The legacy execution option and its CLI/library loop, Path/Work scheduler,
old cohort resume and global graph/evidence wrappers are removed. Historical
schema/identity readers remain for explicit offline migration. RuntimeDeps and
the SDK runner require central state. Removed settings are decoded only to
retain original checkpoint identity; they select no store or exploration rule.

There is no cumulative activation or strategy-revision ceiling. Live worker,
resource and actual request/action/time budgets remain authoritative.

Resource effects distinguish an absent declaration (`None`) from an explicit
empty declaration (`[]`). Confirmed creation/removal uses original evidence;
unspecified effects are reported and prevent successful goal settlement. Owned
read-only providers declare empty effects. Arbitrary response text is never
promoted into cleanup proof. Generic write/HTTP/shell providers without a valid
resource declaration remain unsupported for successful cleanup settlement.
