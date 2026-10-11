# Current evidence reuse validity — specialist mode

This local implementation contract grants no target-testing permission and
changes neither `EvidenceRecord` nor the `EvidenceStore` port.

## Ownership and meaning

- `redteam.evidence` remains the append-only original store.
- `orchestration.evidence_validity` owns explicit reuse bindings, code-grounded
  current projections, the internal `evidence.assess` helper and validity events.
- Existing completion gates still own original provenance, execution identity,
  account/target linkage, conditions, Finding proof and semantic acceptance.
- App selects the helper and evidence writer fence only in the trusted specialist
  profile. Generic dev assembly gains neither. Generic completion validators
  nevertheless reject invalid registered bindings if they are present.

An old observation can remain historically true without supporting a current
claim. A projection never deletes/resets its source, Task, Mission or Graph.
`valid` does not prove authentication, a vulnerability, completion or Control ALLOW.

| Status | Meaning for current support |
| --- | --- |
| `unbound` | No explicit conditions; existing original/completion gates still apply |
| `valid` | All registered conditions match grounded baselines; observed deadlines have not elapsed |
| `stale` | A measured value differs or an explicitly observed deadline elapsed |
| `invalid` | Binding/schema/digest/original identity is invalid, or source is failed, fixture/mock, missing or wrong scope |
| `inconclusive` | Reads/current measured conditions cannot establish reuse |

Only `unbound` and `valid` proceed to the unchanged original/completion gates.
Failed evidence without a binding remains rejected by those gates, not made
successful by `unbound`.

## Helper and append-only records

`evidence.assess` is registered by its owner into the existing local registry;
the native loop has no tool-name branch. Workers can discover its schema through
existing cards/detail lookup. Research/evidence stages read its current projection.

Input: required `evidence_id`; optional `conditions` (maximum 16). Each condition
has `node_id`, `container=attr|fact`, `key`, and `test=unchanged|not_expired`.
Caller-supplied status, baseline or deadline is not accepted. Baselines use the
existing uncapped claim-level grounding reader, same Run/engagement successful
non-fixture Tool sources and measured scalar values. Declared/inferred values
and unknown/timezone-less timestamps cannot establish a condition. A lost
condition is inconclusive, not a refutation of the old observation.

A binding is a SYSTEM EvidenceRecord, source `orchestration.evidence_validity`:
`record_kind=evidence_validity_binding_v1`, `schema_version=1`. Its typed
`ReuseBinding` metadata stores the claim evidence ID, whole-source hash,
conditions and value hashes, not a copied secret/raw result. The deterministic
ID deduplicates the same baseline/conditions. All earlier conditions remain AND
constraints; a narrower later binding cannot erase them.
`PlanningEvidenceStore` refuses changed content under an existing ID in
specialist assembly, without replacing the owning filesystem implementation.

Bindings/events are assessment DATA, not target responses or completion receipts.
The helper changes no Task status, creates no Finding and makes no target request.
Errors carry fixed diagnostics, not sensitive input.

## Consumers and race protection

1. Before specialist planning, `refresh_validity` computes fresh projections
   and atomically queues changed `EvidenceValidityChanged` envelopes in the
   existing Coordinator/outbox. The stored last projection deduplicates events;
   it is never a second completion authority.
2. Planner/native context reads current projections. Semantic review reads only
   projections relevant to its source IDs. Planner meaningful-input deduplication
   includes validity, and its input stamp includes the evidence and graph queries.
3. Step, Task and Mission gates reject stale/invalid/inconclusive support.
   Specialist completion captures relevant binding/node versions before review
   and checks them in the existing atomic settlement callback. ABA restoration
   and a new related binding during review cannot admit the old result. An
   unrelated node update does not invalidate this completion read set.
4. Measured, timezone-aware deadlines are checked at final atomic admission.
   No model invents expiry. A plan that read valid support cannot be adopted
   after its observed deadline; already-stale evidence can still inform a
   proposed revalidation task. Deadline comes from the actual model context
   inside the captured read set, not from an earlier event refresh projection.
5. Reports show current validity beside historical records. Current release
   is withheld if a stored goal's supporting evidence is no longer reusable,
   without changing historical completed labels. Unused sibling evidence is
   not a release blocker merely because it is stale/invalid.

Evidence appends fence the query and exact evidence/binding reference before
store I/O. Lost acknowledgement/cancellation retains the fence even if the
record reached disk; no refund or duplicate execution follows. Graph writes
reuse the existing Run-scoped wrapper. Coordinator callbacks perform no
store/model/network I/O. Events are queued atomically and delivered outside
that boundary; duplicate reads/restart do not create new events/model calls.

## Limits and migration

This is a slice of M5, not all of M5. While native specialist actors are active,
the existing driver waits for their results, coalesced selected events **or the
next explicitly observed valid deadline**. Its optional application callback
reads the existing projection records, refreshes expired support from original
stores and hands actual validity changes to the existing specialist planner.
No fixed periodic model tick, detached model task, second queue or timer service
is added. The event payload is only a notification; canonical Run/engagement,
Mission/lifecycle, source reads, relevant CAS and request ledger still govern.

Fresh start and verified full-cohort resume use the same callback. An overdue
stored valid deadline is rechecked on active resume. Replayed/unchanged events
do not request a new judgment; the planner also retains its meaningful-input,
strategy/activation and shared request limits. Changes during a judgment are
not consumed as reviewed. Expiry during a judgment is refreshed again; final
admission/completion retain their own wall-clock checks.

An empty cohort exits instead of waiting for future expiry. There is no service
waking held/terminal/stopped Runs. Partial/unknown recovery does not enable the
callback; paused/closed Missions and pending operator mutations suppress it.
It does not restart an approval-held actor, release a claim, approve a request
or reset completed Tasks. Unprovable reads/writer fences emit a bounded
`evidence_validity_wakeup_held` diagnostic and schedule no immediate timer
retry. A later existing wakeup or explicit recovery can recheck, but no hidden
polling clears the fence. Missing/naive expiry is never replaced with a TTL.
Decision and completion gates still read fresh state independently of this
notification path. Large-record scanning latency and distributed timers have
not been evaluated.

Original execution reconciliation, unknown effects, claims, approvals, shared
request accounting, max_steps and Control remain intact. Actual generated-code
execution/isolation, actual Mobile/session tests, entered-but-checkpoint-less
recovery and legacy/unknown writer recovery remain unfinished. Versioned
Evidence append and new Graph mutation acknowledgement-loss recovery are
separate, documented in [activation recovery](activation-recovery.md); they do
not prove evidence validity or grant execution. External writers bypassing
selected wrappers and distributed multi-host writers are outside this guarantee.

The bundled profile/common/evidence prompt versions are now `2`; fingerprint
changes are intentional. Use a new Run for this profile. Do not force an old
Run to use new prompts or bypass its saved binding. Older report JSON loads
because added fields default to empty. A SYSTEM binding cannot replace a Tool
original.

## Local tests

`tests/integration/test_evidence_validity_consumers.py` and
`tests/unit/test_planning_evidence.py` use scripted models, synthetic originals,
real native completion/reporting paths, memory/SQLite coordination and real
filesystem evidence for durable cases. They do not prove actual LLM judgment
quality or financial/Mobile target performance.

Coverage includes source preservation; failed/fixture/foreign source refusal;
declared versus measured conditions; unknown timezone and explicit expiry;
all three gates; actual late Task settlement; related/independent updates and
ABA; append cancellation/lost acknowledgement; restart deduplication;
late-plan refusal; historical completion versus current report release; and
public assembly/schema registration.

The additional `tests/unit/test_validity_schedule.py` and
`tests/integration/test_evidence_expiry_wakeup.py` cover memory/SQLite deadline
selection, inactive/operator gates, malformed records, overdue active resume
checks, original preservation, duplicate/change handling, cancellation and
writer-fence holds. The integrated case uses actual planner/atomizer, Task
admission/activation, outbox and shared metering with a scripted provider:
expiry creates a followup while independent synthetic actors remain busy.
It does not claim that real LLM or financial/Mobile target execution was tested.
