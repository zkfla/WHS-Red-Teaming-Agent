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
the stored JSONL lines with their indexed line hashes (not a recomputation
of every original ToolResult payload digest) and flags changed/missing/unindexed ones, exposed
as `redteam evidence verify --run <run-id>`.

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

## Selected P2 runtime-local records (version 1)

No change to original ToolResults or the `EvidenceStore` methods. The
selected profile registers local handlers; original target calls still use
Control/MCP. External tool metadata cannot impersonate SYSTEM-owned records.

- `evidence_validity_binding_v1`: append-only SYSTEM record owned by
  `orchestration.evidence_validity`, with schema version, exact cited
  original/claim receipt ID, original source hashes, grounded condition
  references and captured value hashes. Duplicate capture is idempotent.
  A changed condition yields `stale`; corrupt/known-invalid sources yield
  `invalid`; unavailable/unproved conditions yield `inconclusive`.
  `valid` applies only to the explicitly captured conditions. `unbound`
  retains legacy original/result/completion validation: no reuse conditions
  have been registered, not a failed/expired or a verified/successful source.
  The context carries that meaning explicitly. An assessment binding is not
  mandatory for every historical observation. Control admission and evidence
  assessment remain separate.
- Conditions use one same-run/engagement source-grounded graph snapshot.
  `unchanged` compares hashes. `not_expired` additionally checks a directly
  observed timezone-aware expiry timestamp; no guessed TTL is permitted.
  Refresh is performed at context/claim/fixture checks and reporting. Selected
  durable profiles may inject a bounded known-expiry waiter before a soft
  hold/stall terminates. Deadlines and one `EvidenceValidityExpired` event
  are committed on the existing Coordinator/outbox, not a new timer DB/service.
  Only exact cited bindings of a live held delegation may wake OBSERVE; unknown
  expiry, exhausted allowances, closed missions and changed paths do not.
  Original results/bindings remain immutable. A bound claim receipt can become
  stale without invalidating unrelated claims from the same original.
- Delegated and completion-review context carries bounded operational outcomes
  only for actions linked to the cited evidence or the owning delegation's work.
  Control ALLOW in that history explains a past invocation; it does not approve
  a new invocation or prove a target claim. Source/action filtering and the
  existing redaction boundary still apply. A missing capped history item is
  not proof of DENY or absence.
- `code_artifact_v1`: SYSTEM record owned by `orchestration.code_artifacts`.
  The `artifact_ref` points to canonical JSON in the existing immutable
  filesystem blob backend. `digest` hashes the exact bytes. Version, run,
  engagement, purpose digest, delegation, original source hashes, Python
  stdlib module, JSON fixtures, reviewer prompt binding and fixture policy
  are checked before execution. Review acceptance is not a Control approval.
- `code_fixture_result_v1`: SYSTEM, `fixture_only=true`, linked to the
  artifact and policy hashes. Never a successful original target ToolResult,
  final vulnerability receipt or Finding. Fixture expectations are synthetic
  and may be authored by the model; passing them is not independent proof
  of correctness or exploitability.

The public blob port is `core.ports.artifacts.ArtifactStore`; old
`evidence.artifacts.base.ArtifactBackend`/`ArtifactWriteResult` names are
compatible re-exports. `core.ports.fixture_runner` is deliberately separate
from target execution: fixed JSON fixture inputs, no arbitrary argv/mounts,
an operator-pinned cached image, and typed bounded fixture results.
FixturePolicy v2 keeps expected outputs on the host and compares canonical
JSON candidate outputs there. Sandbox stdout cannot supply passed/total verdicts.
Correct output on finite fixtures is not attestation that a particular function
ran, general algorithm correctness, or actual-target vulnerability proof.
App injects both optional ports only for an explicitly selected profile.
Old profiles have no added local artifact tools. Existing fingerprint/resume
checks require a new Run when the profile, prompts or fixture policy change.
