# Current evidence reuse validity — central runtime

This local implementation contract grants no target-testing permission and
changes neither `EvidenceRecord` nor the `EvidenceStore` port.

## Ownership and meaning

- `redteam.evidence` remains the append-only original store.
- `orchestration.evidence_validity` owns explicit reuse bindings, code-grounded
  current projections, the internal `evidence.assess` helper and completion read sets.
- Existing completion gates still own original provenance, execution identity,
  account/target linkage, conditions, Finding proof and semantic acceptance.
- App selects the helper through the existing specialist profile assembly.
  Central Task and goal completion validate registered conditions independently
  of helper visibility. No legacy Coordinator or separate validity DB is used.

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
Central evidence inspection refuses changed content under an existing ID
without replacing the owning filesystem implementation.

Bindings and registration references are assessment DATA, not target responses
or completion receipts.
The helper changes no Task status, creates no Finding and makes no target request.
Errors carry fixed diagnostics, not sensitive input.

## Completion lifecycle

1. Before Task review, `capture_validity` captures the selected originals,
   append-only bindings, grounding originals and exact measured condition
   values/provenance. Unbound sources retain existing provenance/claim gates;
   no deadline is invented.
2. Goal input checks essential investigations and required follow-up receipts.
   Unusable optional Task support is omitted from the review mapping. Optional
   uncertainty remains in result/validity reporting. A review can select valid
   optional sources; those selected sources then receive the same final checks.
3. After the model response, relevant source/binding hashes and condition
   states are refreshed. New bindings, changed/missing conditions, expired
   support or changed originals prevent stale success from being committed.
4. The final central transaction checks observed deadlines against the current
   clock, compares just the relevant graph predicates/provenance and the binding
   reference index, verifies Task ownership/goal inputs, and commits the state.
   Unrelated predicates, unused optional support and independent optional Task
   updates are not global fingerprints. Required original execution/approval,
   active-job, UNKNOWN and cleanup checks remain in place.
5. Public and native unresolved Task settlement share `complete_unresolved_result`:
   originals, gaps and follow-up questions are retained with the Task result.
   It creates no positive completion receipt and cannot close the Mission.

`evidence.assess` registers a binding reference before evidence file I/O in the
existing `state_requests` table (`kind=evidence_reuse_binding`). The payload is
only the source evidence ID and binding record hash; conditions and bodies remain
in Evidence. A lost acknowledgement/missing binding original is detected by the
completion read set. The transaction never awaits Evidence/model/provider I/O.
Historical unindexed bindings are still inspected from their retained originals;
new helper registrations use the reference index. No new DB/table/queue is added.

## Limits and compatibility

This change fixes final completion checks, not the notification lifecycle.
`refresh_validity` currently projects fresh status on read. Automatic observed
expiry wakeup and planner input fixes remain plan4c work; no timer, expiry daemon
or automatic target revalidation was introduced here. Planning/admission, ROE,
shared usage and exact execution identity remain separate authorities.

Bindings retain `evidence_validity_binding_v1` / schema version 1. Their original
files and historical Task/goal results are not rewritten. Conditional provenance
and explicit deadlines are evaluated when support is reused. A SYSTEM binding
never replaces an original Tool result or grants execution permission.

File storage and SQLite are not one atomic store. Inspections happen outside SQL;
registered references and touched graph conditions are checked at commit.
Unsupported external writers that bypass the selected registration path,
malicious file edits after the last integrity read and distributed writers are
not covered by an atomic cross-store guarantee. No global evidence/graph lock
is introduced to conceal that boundary.

## Local verification

`tests/integration/test_plan4a_completion.py` uses temporary filesystem originals,
SQLite, the actual completion/central commit paths, a controllable clock and
explicit scripted model/provider doubles. It covers Task/goal expiry and related
condition/binding changes during review; final-transaction expiry/reference/graph
changes; independent changes; optional unused versus selected support; and public
versus native unresolved results with sibling isolation. Existing central
completion tests retain execution/cleanup/required-question refusal checks.
These checks make no claim about live target performance or real LLM judgment.
