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
verify a record wasn't altered after the fact (verification itself is a
TODO).

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
