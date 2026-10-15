"""Local-tool crash receipts in the existing EvidenceStore, never a replay queue."""

import json
from datetime import datetime, timezone

from redteam.core.contracts.evidence import EvidenceRecord
from redteam.core.enums import EvidenceSourceType
from redteam.core.ids import EvidenceId, new_id
from redteam.orchestration.coordination import content_hash
from redteam.orchestration.engine import DecisionRuleError
from redteam.orchestration.worker_session import WorkerSession


def call_binding(state, call):
    raw = state.get("worker_session")
    if raw is None:
        return None
    session = WorkerSession.model_validate_json(json.dumps(raw))
    return {
        "generation": session.generation, "message_index": len(session.messages) - 1,
        "assistant_hash": content_hash(session.messages[-1].display_json),
        "arguments_hash": content_hash(call.arguments or {}),
        "instance_id": session.binding.instance_id,
    }


async def prior_result(deps, runtime, call, binding):
    """Return an exact recorded outcome; an unresolved submission stays held."""
    if binding is None:
        return None
    records = await deps.evidence_store.list_for_run(runtime.run_id)
    matching = [record for record in records
                if str(record.engagement_id) == str(runtime.engagement_id)
                and record.source_type is EvidenceSourceType.SYSTEM
                and record.source_id == "orchestration.runtime_tool"
                and record.metadata.get("mission_task_id") == str(runtime.active_task_id)
                and record.metadata.get("model_tool_call_id") == call.call_id
                and record.metadata.get("tool_id") == call.tool_id
                and record.metadata.get("call_binding") == binding]
    if any(record.digest != content_hash(record.metadata) for record in matching):
        raise DecisionRuleError("The local tool receipt failed integrity validation.",
                                code="worker_session_local_receipt_invalid")
    results = [record for record in matching
               if record.metadata.get("record_kind") == "runtime_tool_result"]
    if len(results) > 1:
        raise DecisionRuleError("More than one local outcome is recorded for this call.",
                                code="worker_session_local_receipt_invalid")
    if results:
        return results[0]
    if matching:
        raise DecisionRuleError("The original local-tool submission has no durable outcome; "
                                "recovery is held without replaying its side effects.",
                                code="worker_session_local_outcome_unknown")
    return None


async def record_submission(deps, runtime, call, binding):
    if binding is None:
        return
    payload = {
        "record_kind": "runtime_tool_submission", "tool_id": call.tool_id,
        "model_tool_call_id": call.call_id, "call_binding": binding,
        "mission_id": str(runtime.mission_id), "mission_task_id": str(runtime.active_task_id),
    }
    await deps.evidence_store.append(EvidenceRecord(
        evidence_id=EvidenceId(new_id("evidence")), run_id=runtime.run_id,
        engagement_id=runtime.engagement_id, source_type=EvidenceSourceType.SYSTEM,
        source_id="orchestration.runtime_tool", content_type="application/json",
        digest=content_hash(payload), summary="Local tool submission: " + call.tool_id,
        created_at=datetime.now(timezone.utc), metadata=payload,
    ))
