"""Verified original-call recovery. No tool submission, new queue or budget."""

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

from redteam.core.contracts.action import ActionProposal
from redteam.core.contracts.execution import ExecutionRecoveryAnswer, ExecutionState
from redteam.core.contracts.tool import ToolInvocation, execution_outcome_uncertain
from redteam.core.enums import AgentPhase, PolicyVerdict
from redteam.orchestration.agents.base import PlanStep
from redteam.orchestration.central_runtime import commit_result, runtime_token, tool_limit
from redteam.orchestration.central_state import check_evidence, check_execution_receipt
from redteam.orchestration.coordination import StateConflict
from redteam.orchestration.execution_collection import collect_execution_original
from redteam.orchestration.execution_journal import (
    ExecutionReceiptError,
    append_execution_receipt,
    checked_receipt,
    preserved_result,
)
from redteam.orchestration.execution_reconciliation import ExecutionReconciliation
from redteam.orchestration.nodes.policy import policy_node
from redteam.orchestration.resource_conflicts import execution_resource_keys
from redteam.orchestration.state import initial_graph_state


async def original_resource_keys(deps, execution):
    """Derive comparisons from verified original receipts, never guessed old keys."""
    checked = await check_evidence(
        deps.evidence_store, run_id=deps.central_state.run_id,
        engagement_id=deps.central_state.engagement_id,
        evidence_ids=[str(execution.proposal_evidence_id)],
    )
    task = await deps.central_state.get_task(str(execution.task_id))
    check_execution_receipt(execution, checked[0].record, result=False, task=task)
    data = checked_receipt(checked[0].record)
    invocation = ToolInvocation.model_validate(data["invocation"])
    step = PlanStep.model_validate(data["plan_step"])
    tool = deps.tool_catalog.get(invocation.tool_id)
    if tool is None:
        raise StateConflict("original_execution_tool_not_bound")
    return execution_resource_keys(tool, invocation, step.principal_ref)


async def resume_intent(deps, local, execution):
    """Control-owned exact approval/admission; atomic original intent reservation."""
    runtime, proposal = local["runtime"], local["action_proposal"]
    checked = await check_evidence(
        deps.evidence_store, run_id=deps.central_state.run_id,
        engagement_id=deps.central_state.engagement_id,
        evidence_ids=[str(execution.proposal_evidence_id)],
    )
    task = await deps.central_state.get_task(str(execution.task_id))
    check_execution_receipt(execution, checked[0].record, result=False, task=task)
    data = checked_receipt(checked[0].record)
    if (proposal.model_dump(mode="json") != data["proposal"]
            or local["prepared_invocation"].model_dump(mode="json") != data["invocation"]
            or local["plan_step"].model_dump(mode="json") != data["plan_step"]):
        raise StateConflict("approval_original_proposal_mismatch")
    async with deps.policy_lock:
        await runtime_token(deps, runtime)
        original = await deps.control_plane.recorded_decision(proposal)
        if original is None:
            raise StateConflict("original_control_admission_missing")
        approval = None
        if execution.approval_ref:
            approval = await deps.control_plane.inspect_approval(execution.approval_ref)
        if original.verdict is PolicyVerdict.REQUIRE_APPROVAL:
            await deps.control_plane.register_pending_proposal(execution.approval_ref, proposal)
            local["runtime"] = runtime.model_copy(update={
                "pending_approval_id": execution.approval_ref,
                "pending_action_id": proposal.action_id,
            })
            local = await policy_node(local, deps=deps)
            decision = local["policy_decision"]
        else:
            # Its original ALLOW has already charged the shared Control budget.
            decision = original
            local["policy_decision"] = decision
        if decision.verdict is PolicyVerdict.REQUIRE_APPROVAL:
            return None
        if decision.verdict is PolicyVerdict.DENY:
            await deps.central_state.transaction(lambda tx: tx.update_execution(
                execution.model_copy(update={"state": ExecutionState.CANCELLED,
                                             "wait_reason": None}),
                owner=runtime.work_owner, ownership_version=runtime.task_ownership_version,
                now=datetime.now(timezone.utc).timestamp(),
            ))
            return local
        try:
            await deps.central_state.transaction(lambda tx: tx.resume_execution(
                str(execution.invocation_id), owner=runtime.work_owner,
                version=runtime.task_ownership_version, approval=approval,
                now=datetime.now(timezone.utc).timestamp(), ttl=deps.settings.lease_ttl_seconds,
                max_calls=tool_limit(deps), per_target=deps.settings.per_target_concurrency,
                comparison_keys=execution_resource_keys(
                    deps.tool_catalog.get(execution.tool_id), local["prepared_invocation"],
                    local["plan_step"].principal_ref,
                ),
            ))
        except StateConflict as exc:
            if str(exc) in {"shared_resource_execution_conflict", "central_tool_capacity_occupied",
                            "central_target_capacity_occupied"}:
                await deps.central_state.transaction(lambda tx: tx.wait_for_resource(
                    str(execution.invocation_id), owner=runtime.work_owner,
                    version=runtime.task_ownership_version,
                ))
                return None
            raise
        local["runtime"] = local["runtime"].model_copy(update={
            "termination_reason": None, "pending_action_id": None, "pending_approval_id": None,
        })
        return local


async def collect_original(deps, execution, submission):
    """Use original job status/result or the existing execution recovery port."""
    data = checked_receipt(submission)
    invocation = ToolInvocation.model_validate(data["invocation"])
    if not execution.job_ref:
        return await collect_execution_original(deps, submission)
    previous = await preserved_result(
        deps.evidence_store, run_id=invocation.run_id, action_id=invocation.action_id,
        instance_id=invocation.execution_context.instance_id, work_id=None,
    )
    if previous is not None and not execution_outcome_uncertain(previous[0].metadata):
        return previous
    if deps.long_job_adapter is None:
        return None

    async def fetch():
        status = await deps.long_job_adapter.status(execution.job_ref)
        if status.job_id != execution.job_ref:
            raise ExecutionReceiptError("execution_original_job_mismatch")
        if status.state not in {ExecutionState.SUCCEEDED, ExecutionState.FAILED,
                                ExecutionState.CANCELLED}:
            return None
        result = await deps.long_job_adapter.result(execution.job_ref)
        if ((status.state is ExecutionState.SUCCEEDED) != result.success
                or execution_outcome_uncertain(result.metadata)):
            raise ExecutionReceiptError("execution_resolution_unverified")
        prior = (await deps.evidence_store.get(previous[1])) if previous else submission
        receipt = await append_execution_receipt(
            deps.evidence_store, invocation=invocation,
            proposal=ActionProposal.model_validate(data["proposal"]),
            step=PlanStep.model_validate(data["plan_step"]), work_id=None, result=result,
            reference=deps.execution_recovery.prepare_reference(invocation)
            if deps.execution_recovery is not None else None,
        ) if previous is None else await _job_resolution(
            deps, invocation, data, result, prior, status.state, execution.job_ref,
        )
        return result, receipt.evidence_id

    return await asyncio.wait_for(fetch(), timeout=invocation.timeout_seconds)


async def _job_resolution(deps, invocation, data, result, prior, state, job_id):
    from redteam.core.contracts.execution import ExecutionReference
    from redteam.core.convert import execution_input_digest

    reference = ExecutionReference(
        engagement_id=invocation.execution_context.engagement_id, run_id=invocation.run_id,
        instance_id=invocation.execution_context.instance_id, action_id=invocation.action_id,
        invocation_id=invocation.invocation_id, tool_id=invocation.tool_id,
        input_digest=execution_input_digest(invocation), provider_handle={"job_id": job_id},
    )
    return await append_execution_receipt(
        deps.evidence_store, invocation=invocation,
        proposal=ActionProposal.model_validate(data["proposal"]),
        step=PlanStep.model_validate(data["plan_step"]), work_id=None, result=result,
        reference=reference, resolved_from=prior,
        recovery_answer=ExecutionRecoveryAnswer(state=state, verified=True,
                                                uncertain=False, result_available=True),
    )


async def reconcile_originals(deps, runtime, *, snapshot=None):
    """Inspect/settle originals outside SQL, then commit under current owner CAS."""
    await runtime_token(deps, runtime, admit=False)
    outcome = ExecutionReconciliation(runtime=runtime.model_copy(deep=True))
    task = await deps.central_state.get_task(str(runtime.active_task_id))
    executions = await deps.central_state.list_executions(task_id=str(runtime.active_task_id))
    inventory = {str(item.invocation_id): item for item in executions}
    if snapshot is not None:
        for call in snapshot.values.get("native_calls", []):
            if call.completed or call.dispatch is None:
                continue
            identity = str(call.dispatch.invocation.invocation_id)
            if identity not in inventory:
                outcome.errors[identity] = "checkpoint_call_has_no_original_intent"
    for execution in executions:
        identity = str(execution.invocation_id)
        try:
            checked = await check_evidence(
                deps.evidence_store, run_id=deps.central_state.run_id,
                engagement_id=deps.central_state.engagement_id,
                evidence_ids=[str(execution.proposal_evidence_id)],
            )
            submission = checked[0].record
            check_execution_receipt(execution, submission, result=False, task=task)
            if execution.state in {ExecutionState.SUCCEEDED, ExecutionState.FAILED,
                                    ExecutionState.CANCELLED}:
                if execution.result_evidence_id:
                    checked_result = await check_evidence(
                        deps.evidence_store, run_id=deps.central_state.run_id,
                        engagement_id=deps.central_state.engagement_id,
                        evidence_ids=[str(execution.result_evidence_id)],
                    )
                    check_execution_receipt(execution, checked_result[0].record, result=True)
                outcome.processed_invocations.append(identity)
                continue
            if execution.state is ExecutionState.PENDING:
                # Waits are safe original intents, never ordinary assignment.
                continue
            original = await collect_original(deps, execution, submission)
            if original is None:
                outcome.pending_invocations.append(identity)
                continue
            data = checked_receipt(submission)
            dispatch = SimpleNamespace(proposal=ActionProposal.model_validate(data["proposal"]),
                                       plan_step=PlanStep.model_validate(data["plan_step"]))
            local = initial_graph_state(outcome.runtime.model_copy(update={
                "phase": AgentPhase.VERIFY, "termination_reason": None,
            }))
            local.update(action_proposal=dispatch.proposal, plan_step=dispatch.plan_step,
                         tool_result=original[0])
            updated, _, _ = await commit_result(deps, local, dispatch)
            outcome.runtime = updated["runtime"].model_copy(update={
                "phase": runtime.phase, "termination_reason": runtime.termination_reason,
            })
            outcome.processed_invocations.append(identity)
        except Exception as exc:
            outcome.errors[identity] = (exc.reason_code if isinstance(exc, ExecutionReceiptError)
                                        else str(exc) if isinstance(exc, StateConflict)
                                        else type(exc).__name__)
    return outcome
