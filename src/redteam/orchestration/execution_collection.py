"""Collect already-submitted originals without interpreting or replaying them.

This is also usable with a recorded activation identity and no checkpoint.
Collection is NOT resume, Task completion, claim release or accounting repair.
The provider's existing recovery port is the only source of a missing result.
"""

from __future__ import annotations

import asyncio
from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from redteam.core.contracts.action import ActionProposal
from redteam.core.contracts.evidence import EvidenceRecord
from redteam.core.contracts.execution import (
    ExecutionRecoveryAnswer,
    ExecutionReference,
    ExecutionState,
)
from redteam.core.contracts.tool import (
    EXECUTION_TERMINATED_METADATA_KEY,
    ToolInvocation,
    ToolResult,
    execution_outcome_uncertain,
)
from redteam.core.ids import EvidenceId
from redteam.orchestration.agents.base import PlanStep
from redteam.orchestration.context import RuntimeDeps
from redteam.orchestration.execution_journal import (
    EXECUTION_RECEIPT_KINDS,
    ExecutionReceiptError,
    append_execution_receipt,
    append_execution_termination,
    checked_receipt,
    preserved_result,
)
from redteam.orchestration.state import RuntimeState


class ExecutionOriginal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    invocation_id: str
    state: Literal["known", "held", "invalid"]
    reason_code: str
    original_receipt_id: str | None = None


class ExecutionOriginalCollection(BaseModel):
    """Bounded diagnostics only; no provider handles or raw tool outputs."""
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal[1] = 1
    run_id: str
    instance_id: str | None
    replay_permitted: Literal[False] = False
    claim_release_permitted: Literal[False] = False
    originals: list[ExecutionOriginal]


_SUBMISSION_BINDING = ("invocation", "proposal", "plan_step", "candidate_key",
                       "work_id", "instance_id")


async def collect_execution_termination(
    deps: RuntimeDeps, submission: EvidenceRecord,
) -> EvidenceRecord | None:
    """Read a provider's cessation proof without resolving or retrying the action."""
    data = checked_receipt(submission)
    if data["receipt_kind"] != "execution_submission":
        raise ExecutionReceiptError("execution_collection_requires_submission")
    invocation = ToolInvocation.model_validate(data["invocation"])
    original = await preserved_result(
        deps.evidence_store, run_id=submission.run_id, action_id=invocation.action_id,
        instance_id=invocation.execution_context.instance_id, work_id=data.get("work_id"),
    )
    if (original is not None and execution_outcome_uncertain(original[0].metadata)
            and original[0].metadata.get(EXECUTION_TERMINATED_METADATA_KEY) is True):
        record = await deps.evidence_store.get(original[1])
        if record is None:
            raise ExecutionReceiptError("execution_termination_original_missing")
        return await append_execution_termination(
            deps.evidence_store, submission,
            ExecutionRecoveryAnswer(state=ExecutionState.UNKNOWN, verified=True,
                                    uncertain=True, execution_terminated=True),
            original=record,
        )
    provider = deps.execution_recovery
    raw = data.get("execution_reference")
    if provider is None or raw is None:
        return None
    reference = ExecutionReference.model_validate(raw)
    expected = provider.prepare_reference(invocation.model_copy(deep=True))
    if expected is None or expected.model_dump(exclude={"provider_handle"}) != (
        reference.model_dump(exclude={"provider_handle"})
    ):
        raise ExecutionReceiptError("execution_recovery_input_mismatch")
    answer = await asyncio.wait_for(
        provider.query_execution(reference.model_copy(deep=True)),
        timeout=invocation.timeout_seconds,
    )
    answer = ExecutionRecoveryAnswer.model_validate(answer.model_dump(mode="json"))
    if not answer.verified or not answer.execution_terminated:
        return None
    return await append_execution_termination(deps.evidence_store, submission, answer)


async def collect_execution_original(
    deps: RuntimeDeps, submission: EvidenceRecord,
) -> tuple[ToolResult, EvidenceId] | None:
    """Reuse or append a correlated original, never call ToolGateway.invoke.

    A read failure/invalid original propagates; unavailable or still running
    returns None. Cancellation propagates without clearing any owner state.
    """
    submission = submission.model_copy(deep=True)
    data = checked_receipt(submission)
    if data["receipt_kind"] != "execution_submission":
        raise ExecutionReceiptError("execution_collection_requires_submission")
    invocation = ToolInvocation.model_validate(data["invocation"])
    original = await preserved_result(
        deps.evidence_store, run_id=submission.run_id, action_id=invocation.action_id,
        instance_id=invocation.execution_context.instance_id, work_id=data.get("work_id"),
    )
    prior = await deps.evidence_store.get(original[1]) if original is not None else submission
    if prior is None:
        raise ExecutionReceiptError("execution_recovery_original_missing")
    prior_data = checked_receipt(prior)
    if any(prior_data.get(key) != data.get(key) for key in _SUBMISSION_BINDING):
        raise ExecutionReceiptError("execution_receipt_submission_mismatch")
    if original is not None and not execution_outcome_uncertain(original[0].metadata):
        return original
    reference_raw, provider = data.get("execution_reference"), deps.execution_recovery
    if reference_raw is None or provider is None:
        return None
    reference = ExecutionReference.model_validate(reference_raw)
    expected = provider.prepare_reference(invocation.model_copy(deep=True))
    # The opaque handle is validated by its owner. All common identity and
    # approved-input fields must still match the preserved submission.
    if expected is None or expected.model_dump(exclude={"provider_handle"}) != (
        reference.model_dump(exclude={"provider_handle"})
    ):
        raise ExecutionReceiptError("execution_recovery_input_mismatch")

    async def fetch() -> tuple[ToolResult, ExecutionRecoveryAnswer] | None:
        answer_raw = await provider.query_execution(reference.model_copy(deep=True))
        answer = ExecutionRecoveryAnswer.model_validate(answer_raw.model_dump(mode="json"))
        if (not answer.verified or answer.uncertain or not answer.result_available
                or answer.state not in {
                    ExecutionState.SUCCEEDED, ExecutionState.FAILED, ExecutionState.CANCELLED,
                }):
            return None
        result_raw = await provider.fetch_execution(reference.model_copy(deep=True))
        if result_raw is None:
            return None
        result = ToolResult.model_validate(result_raw.model_dump(mode="json"))
        if (result.invocation_id != invocation.invocation_id
                or result.action_id != invocation.action_id
                or result.tool_id != invocation.tool_id):
            raise ExecutionReceiptError("execution_receipt_result_identity_mismatch")
        if execution_outcome_uncertain(result.metadata):
            return None
        if (answer.state is ExecutionState.SUCCEEDED) != result.success:
            raise ExecutionReceiptError("execution_resolution_unverified")
        return result, answer

    # One query/fetch attempt shares the original configured deadline; no
    # retry loop and no global magic timeout for individual providers.
    fetched = await asyncio.wait_for(fetch(), timeout=invocation.timeout_seconds)
    if fetched is None:
        return None
    result, answer = fetched
    resolved = await append_execution_receipt(
        deps.evidence_store, invocation=invocation,
        proposal=ActionProposal.model_validate(data["proposal"]),
        step=PlanStep.model_validate(data["plan_step"]), work_id=data.get("work_id"),
        reference=reference, result=result, target_node_id=data.get("target_node_id"),
        resolved_from=prior, recovery_answer=answer,
    )
    return result, resolved.evidence_id


async def collect_execution_originals(
    deps: RuntimeDeps, runtime: RuntimeState, *, original_task_id: str | None = None,
) -> ExecutionOriginalCollection:
    """Collect one exact actor inventory, not a whole-worker non-entry proof.

    Caller owns a quiescent Run and verifies its recorded identity/config.
    Work/receipts are correlated before provider reads. Orphan Work stays
    visible in the existing execution diagnostics; an empty collection never
    proves that an actor did not run.
    """
    records = await deps.evidence_store.list_for_run(runtime.run_id)
    assert deps.work_store is not None
    works = await deps.work_store.list_items(run_id=runtime.run_id)
    if (any(record.run_id != runtime.run_id for record in records)
            or len({record.evidence_id for record in records}) != len(records)
            or any(work.run_id != runtime.run_id for work in works)
            or len({work.work_id for work in works}) != len(works)):
        raise ExecutionReceiptError("execution_collection_inventory_invalid")
    work_by_id = {work.work_id: work for work in works}
    submissions: dict[str, list[EvidenceRecord]] = {}
    for record in records:
        if (record.metadata.get("receipt_kind") == "execution_submission"
                and record.metadata.get("instance_id") == runtime.instance_id):
            submissions.setdefault(str(record.invocation_id), []).append(record)
    outcomes = []
    for identity, group in sorted(submissions.items()):
        try:
            data = checked_receipt(group[0])
            if any(checked_receipt(other) != data for other in group[1:]):
                raise ExecutionReceiptError("conflicting_execution_submissions")
            if group[0].engagement_id != runtime.engagement_id:
                raise ExecutionReceiptError("execution_collection_scope_mismatch")
            if (original_task_id is not None
                    and (data["plan_step"].get("mission_task_id") != original_task_id
                         or data["proposal"]["agent_id"] != runtime.current_agent)):
                raise ExecutionReceiptError("execution_collection_task_mismatch")
            work_id = data.get("work_id")
            work = work_by_id.get(work_id) if isinstance(work_id, str) else None
            if work_id is not None and (
                work is None or work.instance_id != runtime.instance_id
                or work.candidate_key != data["candidate_key"]
                or str(work.agent_id) != data["proposal"]["agent_id"]
                or work.mission_task_id != data["plan_step"].get("mission_task_id")
            ):
                raise ExecutionReceiptError("execution_recovery_work_mismatch")
            # Validate related execution receipts before any provider call.
            # Processing seals remain the separate interpreter's responsibility.
            for record in records:
                if (record.invocation_id != group[0].invocation_id
                        or record.metadata.get("receipt_kind") not in EXECUTION_RECEIPT_KINDS):
                    continue
                other = checked_receipt(record)
                if any(other.get(key) != data.get(key) for key in _SUBMISSION_BINDING):
                    raise ExecutionReceiptError("execution_receipt_submission_mismatch")
            original = await collect_execution_original(deps, group[0])
            outcomes.append(ExecutionOriginal(
                invocation_id=identity, state="known" if original is not None else "held",
                reason_code="execution_original_preserved" if original is not None
                else "execution_original_unresolved",
                original_receipt_id=str(original[1]) if original is not None else None,
            ))
        except Exception as exc:  # noqa: BLE001 - isolated, bounded diagnostic; not cancellation
            outcomes.append(ExecutionOriginal(
                invocation_id=identity,
                state="invalid" if isinstance(exc, ExecutionReceiptError | ValidationError)
                else "held",
                reason_code=exc.reason_code if isinstance(exc, ExecutionReceiptError)
                else "execution_receipt_schema_invalid" if isinstance(exc, ValidationError)
                else type(exc).__name__,
            ))
    return ExecutionOriginalCollection(
        run_id=str(runtime.run_id), instance_id=runtime.instance_id, originals=outcomes,
    )
