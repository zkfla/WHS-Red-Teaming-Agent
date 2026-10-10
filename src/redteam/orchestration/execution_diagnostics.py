"""Read-only original execution inventory, never recovery or replay authority.

Providers supply detached snapshots through existing contracts. Original and
processing validation belongs to execution_journal; no provider query, model,
Graph application, Work settlement or accounting mutation runs here.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from redteam.core.contracts.evidence import EvidenceRecord
from redteam.core.contracts.tool import EXECUTION_STARTED_METADATA_KEY, execution_outcome_uncertain
from redteam.core.ids import RunId
from redteam.core.ports.evidence_store import EvidenceStore
from redteam.orchestration.execution_journal import (
    EXECUTION_RECEIPT_KINDS,
    ExecutionReceiptError,
    checked_receipt,
    execution_processed,
    preserved_result,
)
from redteam.orchestration.work.models import WorkItem, WorkStatus

if TYPE_CHECKING:
    from redteam.orchestration.task_activation import ActivationDiagnostic


class ExecutionDiagnostic(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    invocation_id: str | None
    work_id: str | None
    instance_id: str | None
    state: Literal["unknown", "missing_original", "uncertain", "no_entry",
                   "unprocessed", "processed", "invalid"]
    reason_code: str
    work_status: WorkStatus | None = None
    resource_execution_pending: bool | None = None
    activation_matches: bool | None = None
    original_receipt_id: str | None = None
    result_success: bool | None = None
    work_result_matches: bool | None = None
    receipt_count: int = 0


class ExecutionDiagnostics(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal[1] = 1
    run_id: str
    observed_at: datetime
    work_store: Literal["inspected", "unknown"]
    evidence_store: Literal["inspected", "unknown"]
    cross_store_atomic: Literal[False] = False
    replay_permitted: Literal[False] = False
    executions: list[ExecutionDiagnostic]


async def inspect_execution_diagnostics(
    run_id: RunId, *, work_items: Sequence[WorkItem] | None,
    evidence_store: EvidenceStore | None,
    activations: Sequence[ActivationDiagnostic] | None = None,
) -> ExecutionDiagnostics:
    """Classify observed originals, retaining missing, invalid and orphan rows.

    Snapshots are taken independently. Even a matched processed result is
    NOT Task completion, a no-checkpoint restart proof or permission to
    release an occupant. Missing stores never establish non-entry.
    """
    works = {item.work_id: item for item in work_items or ()}
    if len(works) != len(work_items or ()) or any(item.run_id != run_id for item in works.values()):
        raise ValueError("execution_diagnostic_work_inventory_invalid")
    subjects = {item.instance_id: item for item in activations or ()}
    if len(subjects) != len(activations or ()):
        raise ValueError("execution_diagnostic_activation_inventory_invalid")
    groups: dict[str | None, list[EvidenceRecord]] = defaultdict(list)
    records = await evidence_store.list_for_run(run_id) if evidence_store is not None else []
    if (any(record.run_id != run_id for record in records)
            or len({record.evidence_id for record in records}) != len(records)):
        raise ValueError("execution_diagnostic_evidence_inventory_invalid")
    for record in records:
        if record.metadata.get("receipt_kind") in EXECUTION_RECEIPT_KINDS | {"execution_processed"}:
            groups[record.invocation_id].append(record)
    diagnostics = []
    referenced_work: set[str] = set()
    for identity, group in sorted(groups.items(), key=lambda item: str(item[0])):
        first = group[0]
        work_id = first.metadata.get("work_id")
        instance_id = first.metadata.get("instance_id")
        # IDs are a bounded projection; malformed payload IDs are not displayed.
        work_id = work_id if isinstance(work_id, str) else None
        instance_id = instance_id if isinstance(instance_id, str) else None
        referenced_work.update(record.metadata["work_id"] for record in group
                               if isinstance(record.metadata.get("work_id"), str))
        work = works.get(work_id) if work_id is not None else None
        row = ExecutionDiagnostic(
            invocation_id=identity, work_id=work_id, instance_id=instance_id,
            state="unknown", reason_code="work_store_not_inspected",
            work_status=work.status if work else None,
            resource_execution_pending=work.resource_execution_pending if work else None,
            receipt_count=len(group),
        )
        try:
            submissions = [record for record in group
                           if record.metadata.get("receipt_kind") == "execution_submission"]
            if not submissions:
                raise ExecutionReceiptError("execution_submission_missing")
            submission = submissions[0]
            data = checked_receipt(submission)
            if any(record.digest != submission.digest for record in submissions):
                raise ExecutionReceiptError("conflicting_execution_submissions")
            for record in group:
                if record.metadata.get("receipt_kind") == "execution_processed":
                    if (record.action_id != submission.action_id
                            or record.engagement_id != submission.engagement_id
                            or record.metadata.get("instance_id") != data.get("instance_id")
                            or record.metadata.get("work_id") != data.get("work_id")):
                        raise ExecutionReceiptError("execution_processing_identity_mismatch")
                    continue  # execution_processed owns original/result evidence validation
                other = checked_receipt(record)
                if any(other.get(key) != data.get(key) for key in (
                    "invocation", "proposal", "plan_step", "work_id", "instance_id",
                    "candidate_key",
                )):
                    raise ExecutionReceiptError("execution_receipt_submission_mismatch")
            if activations is not None:
                subject = subjects.get(instance_id) if instance_id is not None else None
                row = row.model_copy(update={"activation_matches": bool(
                    subject is not None and subject.engagement_id == str(submission.engagement_id)
                    and subject.role_id == data["proposal"]["agent_id"]
                    and subject.task_id == data["plan_step"].get("mission_task_id")
                    and subject.launch_stage == "entered"
                )})
            if work_items is None:
                diagnostics.append(row)
                continue
            if work_id is not None and (
                work is None or work.instance_id != instance_id
                or work.candidate_key != data["candidate_key"]
                or str(work.agent_id) != data["proposal"]["agent_id"]
                or work.mission_task_id != data["plan_step"].get("mission_task_id")
            ):
                raise ExecutionReceiptError("execution_recovery_work_mismatch")
            assert evidence_store is not None and submission.action_id is not None
            original = await preserved_result(
                evidence_store, run_id=run_id, action_id=submission.action_id,
                instance_id=instance_id, work_id=work_id,
            )
            if original is None:
                if any(record.metadata.get("receipt_kind") == "execution_processed"
                       for record in group):
                    raise ExecutionReceiptError("execution_processing_original_missing")
                row = row.model_copy(update={
                    "state": "missing_original", "reason_code": "execution_original_missing",
                })
            else:
                result, record_id = original
                source = await evidence_store.get(record_id)
                if source is None or result.invocation_id != identity:
                    raise ExecutionReceiptError("execution_recovery_original_missing")
                processed = await execution_processed(evidence_store, source)
                if execution_outcome_uncertain(result.metadata):
                    state, reason = "uncertain", "execution_original_uncertain"
                elif result.metadata.get(EXECUTION_STARTED_METADATA_KEY) is False:
                    state, reason = "no_entry", "execution_original_proves_non_entry"
                elif processed:
                    state, reason = "processed", "execution_original_processing_recorded"
                else:
                    state, reason = "unprocessed", "execution_original_processing_required"
                row = ExecutionDiagnostic.model_validate({
                    **row.model_dump(), "state": state, "reason_code": reason,
                    "original_receipt_id": str(record_id),
                    "result_success": None if execution_outcome_uncertain(result.metadata)
                    else result.success,
                    "work_result_matches": None if work is None
                    or execution_outcome_uncertain(result.metadata) else (
                        work.status is WorkStatus.SUCCEEDED if result.success
                        else work.status in {WorkStatus.FAILED, WorkStatus.CANCELLED}
                    ),
                })
        except (ExecutionReceiptError, ValidationError) as exc:
            row = row.model_copy(update={
                "state": "invalid",
                "reason_code": exc.reason_code if isinstance(exc, ExecutionReceiptError)
                else "execution_receipt_schema_invalid",
            })
        diagnostics.append(row)
    for work_id, work in sorted(works.items()):
        if work_id not in referenced_work:
            diagnostics.append(ExecutionDiagnostic(
                invocation_id=None, work_id=work_id, instance_id=work.instance_id,
                state="unknown", reason_code="work_without_execution_receipts"
                if evidence_store is not None else "execution_store_not_inspected",
                work_status=work.status, resource_execution_pending=work.resource_execution_pending,
                # Work alone has no engagement/original identity to prove the full binding.
            ))
    return ExecutionDiagnostics(
        run_id=str(run_id), observed_at=datetime.now(timezone.utc),
        work_store="inspected" if work_items is not None else "unknown",
        evidence_store="inspected" if evidence_store is not None else "unknown",
        executions=diagnostics,
    )
