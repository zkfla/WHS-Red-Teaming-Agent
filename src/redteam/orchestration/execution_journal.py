"""Immutable execution receipts in the existing evidence store.

Submission precedes provider entry; the original bounded result precedes
publication and interpretation. These receipts record execution only and
are never observations, findings, or permission to repeat an action.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from redteam.core.contracts.action import ActionProposal
from redteam.core.contracts.evidence import EvidenceRecord
from redteam.core.contracts.execution import (
    ExecutionRecoveryAnswer,
    ExecutionReference,
    ExecutionState,
)
from redteam.core.contracts.tool import (
    EXECUTION_STARTED_METADATA_KEY,
    ToolInvocation,
    ToolResult,
    execution_outcome_uncertain,
)
from redteam.core.convert import digest_payload
from redteam.core.enums import EvidenceSourceType
from redteam.core.ids import ActionId, EvidenceId, RunId, new_id
from redteam.core.ports.evidence_store import EvidenceStore
from redteam.orchestration.agents.base import PlanStep

EXECUTION_RECEIPT_KINDS = frozenset({
    "execution_submission", "execution_result", "execution_resolution", "execution_retry_admission",
})


class ExecutionReceiptError(ValueError):
    """Owner-defined bounded reason for a receipt/correlation rejection."""

    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


async def append_execution_receipt(
    store: EvidenceStore,
    *,
    invocation: ToolInvocation,
    proposal: ActionProposal,
    step: PlanStep,
    work_id: str | None,
    reference: ExecutionReference | None = None,
    result: ToolResult | None = None,
    target_node_id: str | None = None,
    accounting_before: dict[str, Any] | None = None,
    resolved_from: EvidenceRecord | None = None,
    recovery_answer: ExecutionRecoveryAnswer | None = None,
    retry_attempt: int | None = None,
) -> EvidenceRecord:
    """Preserve the original before any event that can fail independently."""
    if result is not None and (
        result.invocation_id != invocation.invocation_id
        or result.action_id != invocation.action_id
        or result.tool_id != invocation.tool_id
    ):
        raise ExecutionReceiptError("execution_receipt_result_identity_mismatch")
    payload: dict[str, Any] = {
        "receipt_kind": "execution_result" if result is not None else "execution_submission",
        "proposal": proposal.model_dump(mode="json"),
        "plan_step": step.model_dump(mode="json"),
        "invocation": invocation.model_dump(mode="json"),
        "candidate_key": step.candidate_key,
        "work_id": work_id,
        "instance_id": invocation.execution_context.instance_id,
    }
    if target_node_id is not None:
        payload["target_node_id"] = target_node_id
    if reference is not None:
        payload["execution_reference"] = reference.model_dump(mode="json")
    if accounting_before is not None:
        payload["accounting_before"] = accounting_before
    if retry_attempt is not None:
        if type(retry_attempt) is not int or retry_attempt < 1 or result is not None:
            raise ExecutionReceiptError("execution_retry_receipt_invalid")
        payload.update(receipt_kind="execution_retry_admission", retry_attempt=retry_attempt)
    if result is not None:
        payload["original_result"] = result.model_dump(mode="json")
    if resolved_from is not None:
        prior = checked_receipt(resolved_from)
        if (result is None or reference is None or recovery_answer is None
            or not recovery_answer.verified or recovery_answer.uncertain
            or not recovery_answer.result_available
            or recovery_answer.state not in {
                ExecutionState.SUCCEEDED, ExecutionState.FAILED, ExecutionState.CANCELLED,
            }
            or (recovery_answer.state is ExecutionState.SUCCEEDED) != result.success
            or execution_outcome_uncertain(result.metadata)
            or resolved_from.run_id != invocation.run_id
            or resolved_from.engagement_id != invocation.execution_context.engagement_id
            or resolved_from.action_id != invocation.action_id
            or resolved_from.invocation_id != invocation.invocation_id
            or prior.get("work_id") != work_id
            or prior.get("instance_id") != invocation.execution_context.instance_id):
            raise ExecutionReceiptError("execution_resolution_unverified")
        payload.update(
            receipt_kind="execution_resolution", resolved_from=str(resolved_from.evidence_id),
            resolved_from_digest=resolved_from.digest,
            recovery_answer=recovery_answer.model_dump(mode="json"),
        )
    record = EvidenceRecord(
        evidence_id=EvidenceId(new_id("evidence")),
        run_id=invocation.run_id,
        engagement_id=invocation.execution_context.engagement_id,
        source_type=EvidenceSourceType.AGENT,
        source_id="orchestration.execute",
        action_id=invocation.action_id,
        invocation_id=invocation.invocation_id,
        hypothesis_id=proposal.hypothesis_id,
        content_type="application/json",
        digest=digest_payload(payload),
        summary="Preserved original execution result" if result is not None
                else "Registered original invocation for reconciliation",
        created_at=datetime.now(timezone.utc),
        metadata=payload,
    )
    await store.append(record)
    return record


def checked_receipt(record: EvidenceRecord) -> dict[str, Any]:
    """Validate immutable payload/correlation before consuming a receipt."""
    payload = record.metadata
    if (record.source_type is not EvidenceSourceType.AGENT
            or payload.get("receipt_kind") not in EXECUTION_RECEIPT_KINDS):
        raise ExecutionReceiptError("not_an_execution_receipt")
    if record.digest != digest_payload(payload):
        raise ExecutionReceiptError("execution_receipt_digest_mismatch")
    invocation = ToolInvocation.model_validate(payload.get("invocation"))
    proposal = ActionProposal.model_validate(payload.get("proposal"))
    step = PlanStep.model_validate(payload.get("plan_step"))
    if (record.run_id != invocation.run_id
        or record.run_id != invocation.execution_context.run_id
        or record.engagement_id != invocation.execution_context.engagement_id
        or record.action_id != invocation.action_id
        or record.invocation_id != invocation.invocation_id
        or payload.get("instance_id") != invocation.execution_context.instance_id):
        raise ExecutionReceiptError("execution_receipt_identity_mismatch")
    if (proposal.action_id != invocation.action_id
        or proposal.run_id != invocation.run_id
        or proposal.engagement_id != invocation.execution_context.engagement_id
        or proposal.instance_id != invocation.execution_context.instance_id
        or step.tool_id != invocation.tool_id
        or payload.get("candidate_key") != step.candidate_key):
        raise ExecutionReceiptError("execution_receipt_action_mismatch")
    approved_target = invocation.execution_context.approved_target
    if approved_target is not None and approved_target != proposal.target:
        raise ExecutionReceiptError("execution_receipt_approved_target_mismatch")
    if payload.get("execution_reference") is not None:
        reference = ExecutionReference.model_validate(payload["execution_reference"])
        if (reference.engagement_id != record.engagement_id or reference.run_id != record.run_id
            or reference.instance_id != invocation.execution_context.instance_id
            or reference.action_id != record.action_id
            or reference.invocation_id != record.invocation_id
            or reference.tool_id != invocation.tool_id):
            raise ExecutionReceiptError("execution_receipt_reference_mismatch")
    return payload


async def preserved_result(
    store: EvidenceStore, *, run_id: RunId, action_id: ActionId,
    instance_id: str | None, work_id: str | None,
) -> tuple[ToolResult, EvidenceId] | None:
    """Read the exact action's result, rejecting conflicting originals."""
    found: tuple[ToolResult, EvidenceId] | None = None
    unresolved: tuple[ToolResult, EvidenceId] | None = None
    for record in await store.list_for_action(action_id):
        if (record.run_id != run_id
            or record.metadata.get("receipt_kind") not in {
                "execution_result", "execution_resolution",
            }
            or record.metadata.get("instance_id") != instance_id
            or record.metadata.get("work_id") != work_id):
            continue
        payload = checked_receipt(record)
        result = ToolResult.model_validate(payload["original_result"])
        invocation = ToolInvocation.model_validate(payload["invocation"])
        if (result.action_id != invocation.action_id
            or result.invocation_id != invocation.invocation_id
            or result.tool_id != invocation.tool_id):
            raise ExecutionReceiptError("execution_receipt_result_identity_mismatch")
        if payload["receipt_kind"] == "execution_resolution":
            source = await store.get(EvidenceId(payload.get("resolved_from", "")))
            answer = ExecutionRecoveryAnswer.model_validate(payload.get("recovery_answer"))
            if (source is None or source.digest != payload.get("resolved_from_digest")
                or source.run_id != record.run_id or source.engagement_id != record.engagement_id
                or source.action_id != record.action_id
                or source.invocation_id != record.invocation_id
                or checked_receipt(source).get("instance_id") != instance_id
                or checked_receipt(source).get("work_id") != work_id
                or not answer.verified or answer.uncertain or not answer.result_available
                or answer.state not in {
                    ExecutionState.SUCCEEDED, ExecutionState.FAILED, ExecutionState.CANCELLED,
                }
                or (answer.state is ExecutionState.SUCCEEDED) != result.success
                or execution_outcome_uncertain(result.metadata)):
                raise ExecutionReceiptError("execution_resolution_unverified")
        elif (unresolved is not None and not execution_outcome_uncertain(result.metadata)):
            raise ExecutionReceiptError("execution_resolution_receipt_missing")
        if execution_outcome_uncertain(result.metadata):
            if unresolved is not None and unresolved[0] != result:
                raise ExecutionReceiptError("conflicting_uncertain_execution_results")
            unresolved = (result, record.evidence_id)
            continue
        if found is not None and found[0] != result:
            raise ExecutionReceiptError("conflicting_execution_results")
        found = (result, record.evidence_id)
    return found if found is not None else unresolved


async def mark_execution_processed(
    store: EvidenceStore, *, run_id: RunId, action_id: ActionId,
    instance_id: str | None, work_id: str | None, evidence_id: EvidenceId,
) -> None:
    """Seal processing after graph/evidence/work settlement, without copying raw output."""
    original = await preserved_result(
        store, run_id=run_id, action_id=action_id, instance_id=instance_id, work_id=work_id,
    )
    if original is None:
        # Older results have no execution journal; they keep their existing
        # processing contract rather than inventing an original receipt.
        return
    result, original_id = original
    source = await store.get(original_id)
    assert source is not None
    payload = {
        "receipt_kind": "execution_processed", "instance_id": instance_id,
        "work_id": work_id, "original_receipt_id": str(original_id),
        "original_result_digest": digest_payload(result.model_dump(mode="json")),
        "result_evidence_id": str(evidence_id),
    }
    await store.append(EvidenceRecord(
        evidence_id=EvidenceId(new_id("evidence")), run_id=source.run_id,
        engagement_id=source.engagement_id, source_type=EvidenceSourceType.AGENT,
        source_id="orchestration.execution_reconciliation", action_id=source.action_id,
        invocation_id=source.invocation_id, hypothesis_id=source.hypothesis_id,
        content_type="application/json", digest=digest_payload(payload),
        summary="Original execution result processing completed",
        created_at=datetime.now(timezone.utc), metadata=payload,
    ))


async def execution_processed(store: EvidenceStore, original: EvidenceRecord) -> bool:
    payload = checked_receipt(original)
    result = ToolResult.model_validate(payload["original_result"])
    for record in await store.list_for_action(result.action_id):
        data = record.metadata
        if (data.get("receipt_kind") != "execution_processed"
            or record.run_id != original.run_id
            or record.engagement_id != original.engagement_id
            or record.invocation_id != original.invocation_id
            or data.get("instance_id") != payload.get("instance_id")
            or data.get("work_id") != payload.get("work_id")):
            continue
        if (record.source_type is not EvidenceSourceType.AGENT
            or record.digest != digest_payload(data)):
            raise ExecutionReceiptError("execution_processing_receipt_invalid")
        source = await store.get(EvidenceId(data.get("original_receipt_id", "")))
        if data.get("original_result_digest") != digest_payload(result.model_dump(mode="json")):
            if (source is not None and source.run_id == original.run_id
                and source.invocation_id == original.invocation_id
                and source.action_id == original.action_id
                and checked_receipt(source).get("instance_id") == payload.get("instance_id")
                and source.metadata.get("work_id") == payload.get("work_id")
                and execution_outcome_uncertain(
                    source.metadata.get("original_result", {}).get("metadata", {})
                )
                and payload.get("receipt_kind") == "execution_resolution"):
                # Processing a timeout envelope never settles a later
                # verified original. Keep both immutable and process the
                # newly recovered result through the ordinary consumer.
                continue
            raise ExecutionReceiptError("execution_processing_result_conflict")
        evidence = await store.get(EvidenceId(data.get("result_evidence_id", "")))
        if (source is None
            or checked_receipt(source).get("original_result") != payload["original_result"]
            or evidence is None or evidence.source_type is not EvidenceSourceType.TOOL
            or evidence.action_id != result.action_id
            or evidence.invocation_id != result.invocation_id
            or evidence.run_id != original.run_id
            or evidence.engagement_id != original.engagement_id):
            raise ExecutionReceiptError("execution_processing_evidence_mismatch")
        return True
    return False


async def unsettled_execution_receipts(
    store: EvidenceStore, *, run_id: RunId, instance_id: str | None = None,
    all_instances: bool = False, mission_task_id: str | None = None,
) -> list[str]:
    """Read unresolved execution identities in an explicit completion scope.

    Task completion can inspect its own instance/task; mission completion
    inspects the whole run. This lookup does no provider I/O or execution.
    A malformed original propagates as a bounded receipt error rather than
    being silently removed from the unfinished work set.
    """
    pending: set[str] = set()
    for submission in await store.list_for_run(run_id):
        data = submission.metadata
        if (data.get("receipt_kind") != "execution_submission"
            or not all_instances and data.get("instance_id") != instance_id
            or mission_task_id is not None
            and data.get("plan_step", {}).get("mission_task_id") != mission_task_id):
            continue
        checked_receipt(submission)
        assert submission.action_id is not None
        original = await preserved_result(
            store, run_id=run_id, action_id=submission.action_id,
            instance_id=data.get("instance_id"), work_id=data.get("work_id"),
        )
        identity = str(submission.invocation_id)
        if original is None:
            pending.add(identity)
            continue
        result, record_id = original
        if (result.metadata.get(EXECUTION_STARTED_METADATA_KEY) is False
            and not execution_outcome_uncertain(result.metadata)):
            continue
        source = await store.get(record_id)
        if (source is None or execution_outcome_uncertain(result.metadata)
            or not await execution_processed(store, source)):
            pending.add(identity)
    return sorted(pending)
