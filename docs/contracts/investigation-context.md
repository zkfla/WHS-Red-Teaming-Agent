# Bounded investigation context reads (version 1)

The `investigation_context` module owns read-only Task, candidate, execution and
graph views for model context. The Planner uses these bounded views and optional
structured lookup in plan8b. plan8c connects the same read-only service to the
common worker as a runtime-local tool, `context.query`
(`native_local_tools.py`). Canonical validation snapshots and stored originals
are not replaced by prompt views.

## Snapshot and original state

`capture_investigation_context(state, graph_roots=..., hops=..., max_nodes=...,
max_edges=...)` borrows one central read transaction. It captures the bound
mission, Tasks, candidates and executions, plus the requested graph neighborhood.
Candidate reads use the hypothesis node-type query. Neighborhood reads use
indexed incident-edge branches with a result limit and point node reads; they
do not materialize a whole-graph node/edge mirror.

`InvestigationSnapshot` contains immutable serialized records, original content
hashes, binding identity and graph capture scope. Its fingerprint binds the
Run/Engagement/Mission, mission revision, record contents and neighborhood.
Input model mutations cannot change a captured snapshot. No schema migration,
new database, original result update, model call or target call occurs.

Capturing Tasks/candidates/executions still reads their full canonical inventory.
This stage bounds model-facing output, not the cost of the planning validation
snapshot. The reader's relation index is ephemeral and reconstructed from the
snapshot's existing references; it is not another authoritative graph store.

## Query and response

`ContextQuery` version 1 accepts:

- `kind`: task, candidate, execution, node or edge.
- `mode`: list (optional literal text and typed filters), related (typed roots),
  or read (one matching typed record reference).
- Target IDs, principal references, exact candidate revisions and valid statuses.
- `count` (1–20), `max_bytes` (2048–65536 UTF-8 JSON bytes), `hops` (0–3),
  `max_visited` (1–128) and a bounded continuation.
- An optional expected `snapshot_id`.
- Detail section: record, Task completion_criteria/result, or graph properties.

Input validation rejects unknown fields, booleans used as integer quantities,
foreign physical namespace IDs and incompatible modes/sections. SQL/Cypher and
unbounded natural-language query execution are not supported. Literal text search
finds public title/objective/claim/premise text; it does not decide semantic
duplicates, evidence validity, authorization or vulnerability status.

Example application-side reads:

```python
snapshot = await capture_investigation_context(state, graph_roots=(target_node_id,))
reader = InvestigationContextReader(snapshot)
view = reader.query(ContextQuery.model_validate({
    "kind": "task",
    "mode": "related",
    "refs": [{"kind": "node", "identity": target_node_id}],
    "hops": 1,
    "count": 8,
}))
```

An `ok` response contains scope, snapshot, view kind, selected cards/details,
matching captured count, selected/omitted counts, traversal truncation and
continuation. Count and omission refer to matching records in this response's
captured read scope, not proof of an exhaustive live-graph search. A partial
graph capture is also declared in the summary. Cards declare truncated fields.
They are summaries, not read-back original evidence or independent verdicts.

Continuations bind the query (including limits), snapshot, reader scope and next
offset. They are read cursors, not authorization tokens. Reuse against changed
scope/query/snapshot is an error. Reads against the same immutable snapshot stay
consistent after live state changes; the caller must perform current admission
and related-read/CAS checks before committing a decision. The reader does not
grant a lease or mark planning inputs consumed.

Missing references, unavailable graph details outside the captured neighborhood,
out-of-scope reads, stale requested revisions and invalid continuations are
explicit errors. No empty success is used for these failures. A new graph capture
scope requires an explicitly new snapshot; old continuation pages cannot be
mixed with it. Raw evidence remains accessible through the existing bounded
EvidenceReader contract, which is not duplicated here.

## Overview and mandatory information

`reader.overview(active_task_id=..., feedback_task_ids=..., target_node_ids=...,
count=..., max_bytes=...)` returns finite frontier cards and aggregate counts.
Selection favors the active Task, feedback, source/dependency Tasks, related
candidates, relevant executions, current unsafe executions, selected targets,
required gaps and unfinished work over historical records. Known Task/node update
timestamps order equal-priority records; invocation ordering is not chronology.
Execution recency is explicitly unknown without a captured chronological proof.

The active Task contract preserves its objective, full completion criteria,
principal/target scope, exact admitted revisions, required meaning, source
references, evidence IDs and probe limits. If this mandatory contract cannot fit,
the reader returns `context_required_contract_too_large`. Detail overflow returns
`context_detail_too_large`, without returning a sliced contract as complete.
Callers can read a smaller section or request a larger permitted envelope.

Ordinary cards bound free text, reference collections and nested facts. Aggregate
state/coverage/wait counts and unsafe-execution omissions remain visible even
when open work greatly exceeds the displayed card count. `blocked` is not
terminal. Candidate held/inconclusive is not rejected. `central_completion.unsafe`
owns execution uncertainty: successful job acknowledgments with live job/resource
obligations remain unsafe. Summary omissions never prove goal completion.

## Worker scope and evidence

`InvestigationContextReader(snapshot, worker_task_id=...)` restricts reads to the
assigned Task and forward source/dependency ancestry, their candidates/targets,
associated executions and captured edges between allowed targets. It does not
expose reverse sibling tasks by sharing a target. Ancestry is finite and declares
truncation if its limit is reached. Parent/other graph links do not grant access.

The worker's assigned candidate revision must be present exactly. A newer current
candidate is not substituted for an admitted older snapshot; the reader reports
`context_worker_candidate_revision_unavailable`. Common structured secret
redaction applies before projection/detail delivery. Scope/revision and byte
failures are not candidate verdicts or target failures.

### Native worker tool (plan8c)

`context.query` (`native_local_tools.context_query_handler`) is a runtime-local
tool, declared with `initial_exposure=True` so it is advertised on every native
worker turn alongside `evidence.read`/`task.settle`/`goal.settle`. Its input
schema is `ContextQuery` directly -- no second schema is defined. On each call it
captures a fresh, read-only `InvestigationSnapshot`
(`capture_investigation_context`, 1 hop / 32 nodes / 64 edges) scoped by the
runtime's `active_task_id` and runs `reader.query(...)` against it; it opens no
target/model/write path and never sets `runtime_outcome`. A structured `error`
response (unknown reference, out-of-scope read, stale revision, oversized
reply) is returned as-is; a malformed query or an unexpected store failure is
wrapped in the existing `DecisionDiagnostic` contract
(`cause_scope: model_input`, code preserved from `ContextReadError` where
applicable). It never settles a Task, proposes a verdict or grants scope --
confirmation/refutation still goes through `task.settle`/`goal.settle` and
independent review, unchanged by this tool's existence.

Capturing a fresh snapshot per call means worker lookups are not free: each one
is a full canonical Task/candidate/execution read (the same cost class noted
above for the Planner), not an incremental index. `tests/unit/test_native_context_query.py`
exercises the handler directly against a real temporary central store (own
Task list/read, sibling-Task scope rejection, malformed input, missing active
Task); it does not drive a full native tool-call loop, so a worker actually
*choosing* to call it mid-turn against a real/scripted model is not yet
exercised end to end -- only that the tool is correctly wired, scoped and safe
to expose.

## Planner structured lookup (plan8b)

Both the split strategy profile and the single-stage compatibility profile accept
optional `context_request` version 1. It contains exactly one `context` query (the
contract above) or `evidence` query. A reply with a request cannot also contain
updates, questions/tasks, reviewed IDs, resume conditions or terminal intent.
Normal final replies omit the request or set it to null. `reason` remains required.

```json
{
  "reason": "Find earlier checks of this target",
  "context_request": {
    "version": 1,
    "context": {
      "kind": "task", "mode": "related",
      "refs": [{"kind": "node", "identity": "host:example"}],
      "count": 4, "max_bytes": 16384
    }
  }
}
```

The default request includes up to four cards per kind, automatically hydrated
Task/candidate/execution bodies within 32 KiB, aggregate unfinished/required/wait
counts, bounded knowledge, original/diagnostic windows, and exact-bound public
receipts. Hydrated bodies replace duplicate cards. Mandatory goal, constraints,
operator instructions, roles, readiness, limits and scope remain intact. The
Planner payload envelope has a 128 KiB ceiling; oversized mandatory context fails
explicitly, rather than slicing contracts. This is not a token limit.

Each optional read is followed by another `client.ask` in the **same** decision
budget, request stage, guard and Run ledger. Reads consume normal requests;
invalid mixed replies consume the existing repair budget. Duplicate reads can
reuse the pass-local result cache, but every model request remains charged.
Budget exhaustion reports `planner_lookup_budget_exhausted`, without consuming
the planning cursor, issuing candidate IDs or committing partial Tasks. No extra
read is mandatory. The next payload retains the self-contained base plus only
the latest lookup response, not an accumulating transcript.

`evidence` accepts `mode=list/read`, captured `task_id`, `hypothesis_id` or
`record_kind` filters, `offset`, `count<=20`, `limit<=12000`, optional literal
`query`, and optional expected `snapshot_id`. List returns bounded public record
identities and a next offset. Read requires an evidence ID from this Run's captured
inventory, verifies its unchanged original and uses EvidenceReader's windowing,
provenance and scope checks. Current validity is evaluated with captured bindings
and current conditions. Public receipts expose only their public contract fields;
private/unrecognized receipt metadata is not dumped into the prompt. Default
receipts require exact candidate revision and purpose, and remain limited to four
within 8 KiB; other historical receipts remain discoverable. Neither a card nor a
successful lookup replaces independent review admission.

Reviewed IDs must have actually appeared in a card/detail/lookup. The full snapshot
still owns subject and source authority: a valid omitted source can be supplied
and automatically hydrated for decomposition. Consumed lookup roots, returned
records and bounded traversal dependencies join the existing related Task/node
hashes; exact returned edge/execution hashes are checked in the same atomic commit.
An unrelated sibling update does not invalidate candidate-local judgment. Terminal
inventory/coverage/unsafe gates still inspect full canonical state. No matching
records is a scoped result, not proof of global absence of duplicates.

The atomizer receives an explicit handoff: normalized strategy, issued candidate
IDs, only referenced candidate revisions, complete question criteria and source
Task/result/ancestry, bounded related work/waits and original windows, selected
validity, readiness/tools, limits and constraints. It does not inherit the Planner
payload, model ledger, historical receipts or unrelated history. Mandatory source
material exceeding 64 KiB fails with an explicit diagnostic. Large parallel work
uses bounded conflict/wait cards, not full contracts or an omissions-based gate.
The existing split coverage, paired comparison, source/revision validation, empty
and update-only skips and single atomic admission remain in place.

Default profile version 18 (strategy 9 / atomizer 4 / worker 10) and single-stage
compatibility version 15 (planner 5 / worker 10) bind the current prompts.
The [10b request contract](planner-request.md) keeps independent Planner prefixes
stable and samples current accounting for each lookup/repair attempt. Existing profile
fingerprint/resume checks reject silent changes; no stored Run or checkpoint
is rewritten. [Worker sessions](worker-conversation.md) carry the initial Task
context once, then append new results/changes; explicit input-limit or purpose
generations retain old displays in the existing checkpoint.

## Offline baseline and consumer measurements

`test_investigation_context_baseline.py` captures actual application-level final
messages, composed system prompts and tools/response schemas using explicit
scripted doubles. It checks the shared Run request ledger and the default split
planner/atomizer/native worker route. It sends no provider or target traffic.

The stage8a baseline with 10/100 terminal historical Tasks measured Planner
43,070/143,420 bytes and atomizer 35,874/136,224 bytes; the native worker measured
36,705 bytes and received only its own Task. These are preserved before values,
not a second implementation path. The 8b/8c fixture now exercises 10/100/1000 Task
inventories, both with direct judgment and with two optional Planner lookups,
captures **every** final request envelope (Planner, atomizer and the native
worker's own `complete_native` call, now including the `context.query` tool
schema) and checks summed bytes plus Run ledger requests. Unknown usage stays
unknown. Additional integration fixtures cover omitted terminal candidates,
exact source followups, original public receipt reads, invalid/mixed/stale
reads, finite budgets, related source drift and sibling updates.

Latest offline envelopes (bytes; 10/100/1000 historical Tasks all measured flat
at the same per-stage size):

| Stage | Direct (no lookup) | With two Planner lookups |
| --- | ---: | ---: |
| Planner base | 44,594–44,608 | 44,594–44,608 |
| Atomizer | 25,645–25,657 | 25,645–25,657 |
| Native worker (own Task + `context.query` schema) | 39,238 | 39,238 |
| All requests, cumulative | 109,477–109,503 (3 calls) | 204,704–204,774 (5 calls) |

The before fixture at 100 Tasks sent 316,349 bytes across three requests
(pre-8b). The native worker's own request grew from the 8a baseline's 36,705
bytes to 39,238 bytes (+2,533 bytes, ~6.9%) once `context.query` is advertised
on every turn, even when the worker never calls it -- this is the plan8c
schema-exposure cost; it is otherwise still flat with historical Task count
(the pre-existing `context_for_probe` per-Task filtering, unchanged here).
`probes.context_for_probe` previously also repeated the exact same Task body
under both `active_task` and `investigation_tasks` (1,364 bytes of pure
duplication, found and removed after the first 8c measurement);
`investigation_tasks` now carries only `{task_id, see: "active_task"}` for the
worker, since nothing reads its body separately from `active_task`
(`test_probe_workers.py` only checks `task_id`/list length). `probe.purpose`/
`probe.return_conditions` still duplicate `active_task.objective`/
`completion_criteria` -- left as is because `worker.md`'s prompt text names
"return conditions" directly, so removing the field needs a matching prompt
edit and version bump, not attempted here.
Optional Planner lookup round trips add about 95 KiB in this fixture, so
lookup should be requested only when needed. A worker-initiated `context.query`
round trip (an extra `complete_native` call) is not yet in this cumulative
fixture -- see "Native worker tool (plan8c)" above; `test_native_context_query.py`
verifies the handler directly, not a full tool-call loop. These are transmitted
JSON envelope sizes, not exact tokens or billed savings.

Only scalar sizes/counts are written to the test's temporary artifact. UTF-8 JSON
bytes measure transmitted application structure, not model tokens, cache discounts
or monetary costs. Exact provider serialization may differ. Unknown usage stays
unknown. Real-model exploration quality, provider cache behavior and billed
savings require separate authorized measurement. Full canonical
Task/candidate/execution/node capture and in-memory relationship scans remain:
bounded model input does not imply constant DB/query cost, and each
`context.query` call is a fresh full canonical read, not an incremental index.
