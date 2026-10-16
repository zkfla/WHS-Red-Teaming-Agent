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
into graph facts. The built-in HTTP request mapper bundle is version 3 so the
normal extension fingerprint changes for new assemblies. Incompatible stored
bindings must fail normal resume checks, while old records remain readable.
No historical originals are backfilled with anonymous receipts.

For a fully verified local web deployment with a selected direct Kali route,
the `web-http` provider is replaced at composition time without changing its
public server id, canonical ToolIds, operations, or request schemas. Its v1
`http_receipt` binds the request method, canonical origin/path/query digest,
allowed header names, body digest/size, principal/session scope and redirect
policy to an independent response receipt. The response records whether a
response was received, status, safe header projection, body digest/size and
retention, pinned peer/transport, and a structured failure phase. Credentials
travel only in the provider's bounded stdin envelope and are not copied to the
receipt, audit, telemetry, or model output; the protected transport spool is
discarded after projection, and an unverified discard fails the operation with
an uncertain execution marker. The current body retention mode is `digest_only`;
an operator-enabled bounded, redacted `body_preview` remains untrusted DATA in
the original Evidence window. `artifact_ref` is explicitly null until a
configured immutable response-artifact store owns that content.

The v3 consumer validates both receipt halves and their session/anonymous
scope before graph projection. A declared but contradictory receipt is not
interpreted through the legacy summary path. Transport timeout, TLS/connect,
partial response and out-of-scope redirect results remain failed originals and
cannot create an HTTP graph observation. Arbitrary shell/curl stdout has no
web-http mapper registration and therefore cannot manufacture this receipt.

Successful observation coverage still needs the normal independent Task
review. It does not confirm a candidate, create a Finding, or satisfy the
Mission; those retain their existing revision/purpose and evidence gates.
A posthoc security review receipt is immutable evidence and grants no write by
itself. Its canonical consumer is the Run-bound central review application
service, rather than a later Planner verdict. Central admission cross-checks the
receipt against its inspected originals, current validity, candidate
revision/purpose, target/principals, source origins and optional stored draft.
It then applies the candidate transition and shared promotion gate atomically in
Central SQLite. Promotion does not imply Task or Mission completion.

The Evidence Store receipt and Central application are separate durable
boundaries. A version-3/4 receipt may therefore have `pending_task_result` until
its exact Task result/draft is canonical. Resume retries only an explicit
current-Run pending application. `inconclusive` means the final review did not
settle the claim; `refused` means a structural gate permanently rejected that
application; `retryable_error` identifies a bounded preparation/storage failure.
These states do not create a Finding. Merely opening, inspecting or reporting a
Run performs no application, retry or migration, and historical receipts are
never bulk-promoted.

Version-4 receipts preserve a numeric review `confidence` in the range `0.0..1.0`.
Central admission requires `confidence >= 0.80` before a supported security claim
can confirm its candidate and create a Finding. Lower confidence is stored as an
inconclusive application. Version-2/3 receipts remain readable under their original
categorical contract.

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

### Offline field inspection

`EvidenceReader.read_field` version 2 shares `resolve_output_field` and
`validate_raw_path` (`core/evidence_paths.py`) with assessment storage validation.
It accepts scalar, object and array references.
It remains unavailable as a new autonomous agent tool, but the existing posthoc
claim reviewer uses it for paths declared by a typed assessment draft. Candidate
admission and Finding promotion still consume stored originals and receipts, not
this display projection.

The API requires Run and Engagement identity, accepts an optional exact stored
target identity, and reads one output field with the shared
output-relative/optional leading `output` path rule. Paths have at most 32
segments of at most 512 characters each; negative indices are rejected. Text is
fully redacted before any optional line filter or character slice. Offsets apply
to that redacted, filtered field. The envelope reports total/filtered character
counts, displayed range, omitted leading/trailing characters and display
truncation; those counts do not describe the raw network body. Query text is not
echoed. A query miss or partial window does not establish original-wide absence.
The default field allowance is 4000 characters, capped by the existing 20000
character reader maximum. This is a single-source API with no batch request or
automatic follow-up read.

Empty text, unavailable/redacted fields, absent originals, foreign scope,
and non-TOOL sources have distinct statuses. Structured fields return `status=ok`,
`field_kind=structured`, empty `text` and `output_fields`: at most eight scalar
leaves, each with its full source path, displayed character ranges and omissions.
The same text budget applies across all shown leaves; JSON objects are never
sliced into apparent standalone values. Offset/query filtering requires a scalar
leaf path. Optional assessment `text_span` is checked by the shared resolver and
selects that exact redacted text range; non-text/out-of-bounds spans have explicit
error statuses in both storage and review. Storage retention
and transformation flags can be inspected by their own field paths: the API
does not reconstruct missing content from digests or reinterpret preview
truncation as display completeness. It preserves source success/uncertainty as
display data and always reports `execution_verified=false`; stdout JSON remains
text. Source digests are navigation data, not an integrity check. No source,
receipt, checkpoint, stored hash or verdict is rewritten by inspection.

Posthoc assessment review input is a valid structured envelope, rather than a
2,000-character slice of a JSON document. It prioritizes observation and
expectation paths, shows at most four windows per source and eight sources, and
uses a 12,000-character aggregate text allowance. Each window retains its path,
range, lengths and omission flags. Explicit support/refute windows and
evidence-derived expectation windows are required. Context-only historical
observation drafts retain required-source semantics; when observations explicitly
name support/refute windows, other context windows may be unavailable without
invalidating those essential windows. Missing/foreign originals, target/account
proof failures and missing required fields remain UNKNOWN. Required windows omitted
by source/window/text limits also remain UNKNOWN before any meaning-model call.
Unreadable optional windows are shown with their status and limitations. Sources
without selected paths get a bounded scalar-leaf projection. Generic execution meaning review uses
bounded scalar-field windows (first and last fields when needed), so later fields
cannot be hidden merely by a long earlier object. Task and goal completion review
uses the same scalar-field projection instead of slicing the first 2,000 characters
of serialized raw JSON. Long scalar fields receive explicit first and last windows;
field paths, ranges and omitted counts stay visible. None of these projections
reconstructs digest-only or provider-truncated content.

The verifier attaches code-captured `source_review` version 2 to the public receipt,
including the exact draft binding (when supplied), source digest, paths/spans,
shown windows, truncation, omissions and unavailable required-window counts. The
meaning model receives only the claim and bounded excerpts, without full execution
or approval envelopes. Receipt storage does not reconstruct or upgrade omitted
content. Candidate revision/purpose and reviewed Finding detail gates remain
unchanged; readable fields and Worker assertions grant no verdict or Finding.

Task completion checks execution originals separately from recognized SYSTEM
review/diagnostic records. The latter may remain cited for condition checks but
cannot count as an execution original or enter its raw-result proof path. A
receipt-only completion still lacks required tool originals; directly proving
SYSTEM evidence returns `origin_record_not_execution`, not `origin_raw_missing`.
Unknown SYSTEM formats and foreign-scope records are not silently ignored.
The assembled planning binding includes `evidence_field_contract_version=2`,
including custom profiles, so old Runs remain readable but cannot resume under
changed review semantics. Existing drafts and receipts are not rewritten.

Planner and terminal reports receive bounded `runtime_records` with total and
omitted counts, separately from Task outcomes, original evidence references
and authoritative execution rows. Reports stored before this additive field
still load with an empty view. Historical records are not rewritten. The
evidence-read contribution/descriptor is version v4 and task-settlement remains v3;
existing selection fingerprint checks govern execution resume compatibility.

## Program originals and protected network linkage

Shell execution accounting remains counts/codes only. Original execution windows
add `execution` (success, uncertainty, error, declared subject, accounting and
companion reference) and `program_output` (masked stdout/stderr previews with
separate display-truncation flags). Text and both preview channels share the
same character allowance; offset/query reads retain their JSON-window semantics.
Provider `truncated`/`timed_out` flags are independent of display truncation.
Full originals and artifacts stay in the existing store/read paths. Neither
streams nor private reasoning are added to report execution summaries.

The existing SYSTEM `window_egress_admission` record is displayable as
`view_kind=network_admission`, `execution_original=false`. Shared source/kind
constants live in the network contract; the Capability source re-export remains
compatible. Original windows mark a companion `linked` only with a matching
Run/engagement, exact action/invocation holder, validated companion and payload
digest. Missing, foreign or altered companions stay `missing`/`unbound`.
Route/context identities are references into the companion, not repeated large
configuration snapshots. Missing metering values remain null, never zero.
`linked` preserves UNKNOWN settlement too: it is not a successful network call.
The same bounded views reach subsequent native worker requests and Planner
`original_observations`, retaining original provenance and declared target.

Gate proof establishes metering and destination attempts, not a reached peer,
HTTP/session receipt or the truth of stdout. Shell envelopes cannot be recorded
as measured remote graph facts even with a declared target or companion. A
remote claim based on program output additionally needs the exact protected
route binding before independent claim review; local-only output cannot ground
a remote Finding. Targeted program assertions may support hypotheses/review,
but exit zero, execution success and arbitrary stdout JSON alone prove no
candidate verdict, Finding, Task or Mission completion. The graph-record
contribution/descriptor changes to version 3; normal fingerprint checks reject
incompatible execution resume and historical originals remain readable.

## Native worker observation delivery

`nodes.react._result_messages` renders only the NEW completed tool-result batch.
Automatic original windows in that batch use the existing 8000-character
allowance, at most 4000 characters per window. A later batch does not recalculate
an earlier display. Repeated
references are not repeated independent proof. Windows retain scope/provenance,
source IDs, offsets, actual shown limits, truncation and untrusted DATA semantics;
execution success/UNKNOWN flags and the separate evidence-validity context remain
available. Missing or truncated content is unknown, never inferred success.

The model can request further offsets/queries through the existing EvidenceReader.
Successful reads carry a source `evidence_read_request` in LocalToolResult.
The first actual masked response, including bounded text/program streams and
request ID/offset/limit/query, is frozen in `worker_session` in the existing
instance checkpoint. Native local result receipts preserve that same bounded
reader response. Replay uses the frozen display, not a new read of the original.
Further reads append new call/result pairs. Whole originals remain exclusively
authoritative in EvidenceStore, and a displayed excerpt is never a review receipt.

Normal turns preserve all earlier call/result pairs and opaque continuations;
there is no fixed-round trimming. At an actual input bound a safe, explicit
generation can replace reader bodies with original references and omission
markers; the exact earlier displays stay archived in the same checkpoint.
Unsafe pending/opaque boundaries hold. No second DB or semantic summary store
is introduced. Discovery cannot grant permission, settle a Task, or turn a
partial observation into a Finding. See [Worker conversation](worker-conversation.md).
