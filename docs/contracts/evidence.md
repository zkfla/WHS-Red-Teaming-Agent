# Evidence

Schema: `redteam.core.contracts.evidence.EvidenceRecord`. Store:
`redteam.core.ports.evidence_store.EvidenceStore` (filesystem
implementation: `redteam.evidence.stores.filesystem.FilesystemEvidenceStore`).
Conversion helper: `redteam.core.convert.tool_result_to_evidence`.

## Record

```python
class EvidenceRecord(BaseModel):
    evidence_id: EvidenceId
    run_id: RunId
    engagement_id: EngagementId

    source_type: EvidenceSourceType  # TOOL | AGENT | SYSTEM | OPERATOR
    source_id: str

    action_id: ActionId | None = None
    invocation_id: InvocationId | None = None
    hypothesis_id: HypothesisId | None = None

    content_type: str
    artifact_ref: str | None = None
    digest: str

    summary: str | None = None

    created_at: datetime
    metadata: dict[str, Any] = {}
```

## Append-only

`FilesystemEvidenceStore` writes one JSONL file per run
(`<root>/<run_id>.jsonl`) and only ever appends via `EvidenceStore.append`
— no in-place rewrite. `digest` is a SHA-256 of the source payload
(`redteam.core.convert.digest_payload`), so downstream consumers can
verify a record wasn't altered after the fact. Verification itself is
real, not a TODO: `FilesystemEvidenceStore.verify_run_integrity` compares
indexed JSONL line hashes and flags changed/missing/unindexed records, exposed
as `redteam evidence verify --run <run-id>`. That file-integrity check does not
recompute `EvidenceRecord.digest` against its original source payload.

## Producing evidence from a ToolResult

```python
from redteam.core.convert import tool_result_to_evidence

evidence = tool_result_to_evidence(
    result, run_id=run_id, engagement_id=engagement_id,
    hypothesis_id=proposal.hypothesis_id, summary=proposal.expected_observation,
)
await evidence_store.append(evidence)
```

or via Control Team's convenience wrapper (same conversion underneath):

```python
evidence = await evidence_service.record_tool_result(run_id, engagement_id, result)
```

## Reading evidence back

```python
records = await evidence_store.list_for_run(run_id)
records = await evidence_store.list_for_action(action_id)
record = await evidence_store.get(evidence_id)
```

## Swapping the backend

Implement `EvidenceStore` (`append`, `get`, `list_for_run`,
`list_for_action`) against S3/object storage/a database and wire it in
`redteam.app.bootstrap.build_runtime_deps`.

## Current reuse conditions

Specialist mode adds append-only explicit conditions and fresh validity
projections without changing the store port or rewriting originals.
See [evidence reuse validity](evidence-reuse-validity.md) for status meanings,
consumers, writer fences, migration and limits. Validity is not a vulnerability
verdict or execution permission.

## Actual principal use and anonymous observations

`orchestration.session_proof` owns actual-use receipt interpretation. A
successful original request unit with an explicit `actual_session_ref: null`
can prove anonymous session use. An absent key is unknown. Request echoes,
configured session inventories and declared principal metadata do not prove
use. Failed or mock/fixture originals cannot provide anonymous coverage.
Nullable and named receipts must agree across original and derived layers;
malformed receipts and contradictory values on the same unit are rejected.
In comparisons, each side contributes only its own receipted account or
anonymous use; an unknown side cannot borrow another side's receipt.

For fixed v2 Task conditions, explicit criterion `principal_refs` select the
required subjects; a condition without them inherits the Task subjects. This
scope is computed for validation without rewriting criteria or their stored
identity/hash. Each condition selects coverage from verified original units,
so anonymous coverage cannot disable named-account or paired coverage checks.
Named principal declarations also need their own actual-use receipt or the
existing alias binding grounded in a receipted session.

The HTML inventory producer forwards its own `CapturedResponse.summary()`
without a response body preview or extra request. `has_session_receipt=False`
leaves session use unknown, including synthetic captures. Matching body hashes
do not transfer another invocation's identity or session proof.

HTTP observation metadata comes from the selected registered result mapper,
including catalog-bound physical ToolIds. Bounded untrusted body previews do
not prevent status/header/identity projection; their contents are not copied
into graph facts. The built-in HTTP request mapper bundle is version 2 so the
normal extension fingerprint changes for new assemblies. Incompatible stored
bindings must fail normal resume checks, while old records remain readable.
No historical originals are backfilled with anonymous receipts.

Successful observation coverage still needs the normal independent Task
review. It does not confirm a candidate, create a Finding, or satisfy the
Mission; those retain their existing revision/purpose and evidence gates.

## Runtime diagnostic and review windows

`CompletionIssues` retains compatibility error strings and producer-owned
`DecisionDiagnostic` entries. Settlement returns the entries in
`LocalToolResult.payload.diagnostics`, including cause scope, owner, repair
condition, retryability and scoped evidence/criterion references. Unknown
causes remain unknown. Invalid handler result contracts are runtime defects;
invalid model arguments are repairable input failures. A rejection is not a
refutation of independently successful original observations.

The generic local-call path stores these results in the existing Evidence Store
as SYSTEM `runtime_tool_result` records under `metadata.result`. EvidenceReader
provides masked, bounded windows for recognized runtime results, unresolved
notes, completion reviews/receipts and public review receipts. It marks each
window with `view_kind`, `execution_original` and provenance, enforces Run and
optional engagement binding, and reports truncation and unsupported/absent
content explicitly. Native runtime reads supply both scope identities.

Display support never changes `_RAW_KEYS`, `raw_from_metadata` or `locate_raw`.
Readable diagnostic/review/unresolved records still have no execution original.
The existing purpose/revision, original-linkage and final-admission checks
remain mandatory when consuming public review receipts as claim support.
`unbound` means no current reuse binding; it neither proves nor refutes an
observation. Repeating a read does not create an original or a validity binding.

Planner and terminal reports receive bounded `runtime_records` with total and
omitted counts, separately from Task outcomes, original evidence references
and authoritative execution rows. Reports stored before this additive field
still load with an empty view. Historical records are not rewritten. The
evidence-read and task-settlement contributions/descriptors are version v3;
existing selection fingerprint checks govern execution resume compatibility.

## Native worker observation delivery

`nodes.react._result_messages` rehydrates windows for every retained executed
tool result, including results before a local discovery/read round. Unique
originals and explicit reader requests share the existing 8000-character
automatic-delivery allowance, at most 4000 characters per window. Repeated
references are not repeated independent proof. Windows retain scope/provenance,
source IDs, offsets, actual shown limits, truncation and untrusted DATA semantics;
execution success/UNKNOWN flags and the separate evidence-validity context remain
available. Missing or truncated content is unknown, never inferred success.

The model can request further offsets/queries through the existing EvidenceReader.
Successful reads carry a source `evidence_read_request` in LocalToolResult.
Native checkpoint feedback, its runtime-result record and requested_evidence
context retain the request/metadata and `text_omitted`, not window text. Provider
delivery rehydrates that text from the single original store. The original
read request is retained even if the delivery allowance shortens its shown
window; the returned limit/truncation describe the delivered content. Reference
rehydration makes no target call or additional model request.

The existing complete-round history trimming and assistant/tool-call pairing
remain unchanged. Older windows outside retained rounds remain available through
the probe's original references and explicit reader. No body checkpoint/cache or
second summary store is introduced. Discovery cannot grant permission, settle a
Task, or turn a partial observation into a Finding.
