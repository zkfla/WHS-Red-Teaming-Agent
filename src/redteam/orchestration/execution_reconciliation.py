"""Collect recorded executions and settle processing without resubmitting them."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, cast

from redteam.core.contracts.action import ActionProposal
from redteam.core.contracts.tool import (
    EXECUTION_STARTED_METADATA_KEY,
    ToolInvocation,
)
from redteam.core.enums import AgentPhase
from redteam.orchestration.agents.base import PlanStep
from redteam.orchestration.context import RuntimeDeps
from redteam.orchestration.execution_collection import collect_execution_original
from redteam.orchestration.execution_journal import (
    ExecutionReceiptError,
    checked_receipt,
    execution_processed,
    mark_execution_processed,
)
from redteam.orchestration.nodes.update import update_node
from redteam.orchestration.nodes.verify import verify_node
from redteam.orchestration.state import RuntimeState, initial_graph_state
from redteam.orchestration.work.models import WorkStatus


@dataclass
class ExecutionReconciliation:
    runtime: RuntimeState
    pending_invocations: list[str] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)
    processed_invocations: list[str] = field(default_factory=list)

    @property
    def ready(self) -> bool:
        return not self.pending_invocations and not self.errors


async def reconcile_execution_receipts(
    deps: RuntimeDeps, runtime: RuntimeState,
) -> ExecutionReconciliation:
    """Read originals before constructing a fresh safe reasoning turn.

    Every unresolved submission remains held. Verified terminal execution
    still goes through ordinary evidence/claim processing; it is never
    equated with an observation or achieved objective here.
    """
    outcome = ExecutionReconciliation(runtime=runtime.model_copy(deep=True))
    records = await deps.evidence_store.list_for_run(runtime.run_id)
    submissions = sorted(
        (record for record in records
         if record.metadata.get("receipt_kind") == "execution_submission"
         and record.metadata.get("instance_id") == runtime.instance_id),
        key=lambda record: record.created_at,
    )
    seen: dict[str, str] = {}
    charges: dict[str, tuple[int, int, str]] = {}
    baseline = None
    for submission in submissions:
        identity = str(submission.invocation_id)
        try:
            data = checked_receipt(submission)
            if identity in seen:
                if seen[identity] != submission.digest:
                    raise ExecutionReceiptError("conflicting_execution_submissions")
                continue
            seen[identity] = submission.digest
            if baseline is None:
                baseline = data.get("accounting_before")
            invocation = ToolInvocation.model_validate(data["invocation"])
            proposal = ActionProposal.model_validate(data["proposal"])
            step = PlanStep.model_validate(data["plan_step"])
            retry_admissions = set()
            for record in records:
                if (record.metadata.get("receipt_kind") != "execution_retry_admission"
                    or record.invocation_id != invocation.invocation_id
                    or record.metadata.get("instance_id") != runtime.instance_id):
                    continue
                retry_data = checked_receipt(record)
                attempt = retry_data.get("retry_attempt")
                if (type(attempt) is not int or attempt < 1
                    or retry_data.get("work_id") != data.get("work_id")
                    or retry_data.get("candidate_key") != step.candidate_key
                    or retry_data.get("invocation") != data["invocation"]):
                    raise ExecutionReceiptError("execution_retry_receipt_invalid")
                retry_admissions.add(attempt)
            if retry_admissions != set(range(1, len(retry_admissions) + 1)):
                raise ExecutionReceiptError("execution_retry_receipt_sequence_invalid")
            reserved_retries = len(retry_admissions)
            charges[identity] = (1, reserved_retries, str(proposal.agent_id))
            work_id = data.get("work_id")
            assert deps.work_store is not None
            work = await deps.work_store.get(work_id) if work_id is not None else None
            if work_id is not None and (
                work is None or work.run_id != runtime.run_id
                or work.instance_id != runtime.instance_id
                or work.candidate_key != step.candidate_key
            ):
                raise ExecutionReceiptError("execution_recovery_work_mismatch")
            # A submission with no proved result reserves a conservative
            # action charge; it does not assert that a request reached a peer.
            original = await collect_execution_original(deps, submission)
            if original is None:
                outcome.pending_invocations.append(identity)
                continue
            result = original[0]
            executed = int(result.metadata.get(EXECUTION_STARTED_METADATA_KEY) is not False)
            retries = result.metadata.get("retry_attempts", 0)
            if type(retries) is not int or retries < 0:
                raise ExecutionReceiptError("execution_recovery_retry_accounting_invalid")
            charges[identity] = (executed, max(retries, reserved_retries), str(proposal.agent_id))
            source = await deps.evidence_store.get(original[1])
            if source is None:
                raise ExecutionReceiptError("execution_recovery_original_missing")
            if await execution_processed(deps.evidence_store, source):
                outcome.processed_invocations.append(identity)
                continue
            processing_runtime = outcome.runtime.model_copy(update={
                "phase": AgentPhase.VERIFY, "termination_reason": None,
            })
            replay = initial_graph_state(processing_runtime)
            replay.update({
                "action_proposal": proposal, "plan_step": step, "current_work_id": work_id,
                "tool_result": result.model_copy(update={"metadata": {
                    **result.metadata, "execution_reconciled": True,
                }}),
            })
            reviewed = await verify_node(replay, deps=deps)
            outcome.runtime = reviewed["runtime"].model_copy(update={
                "phase": runtime.phase, "termination_reason": runtime.termination_reason,
            })
            updated = await update_node({**reviewed, "current_work_id": None}, deps=deps)
            evidence_id = reviewed.get("result_evidence_id")
            if evidence_id is None:
                raise ExecutionReceiptError("execution_recovery_evidence_missing")
            processed_result = reviewed["tool_result"]
            verified_success = (
                processed_result is not None and processed_result.success
                and not processed_result.metadata.get("verification_exception")
            )
            if work is not None and not await deps.work_store.reconcile_execution(
                work.work_id, run_id=runtime.run_id, instance_id=runtime.instance_id,
                candidate_key=step.candidate_key,
                status=WorkStatus.SUCCEEDED if verified_success else WorkStatus.FAILED,
                now=datetime.now(timezone.utc),
            ):
                raise ExecutionReceiptError("execution_recovery_work_unsettled")
            await mark_execution_processed(
                deps.evidence_store, run_id=runtime.run_id, action_id=proposal.action_id,
                instance_id=runtime.instance_id, work_id=work_id, evidence_id=evidence_id,
            )
            processed_runtime = updated["runtime"]
            # Verification may spend real model requests. Retain those
            # counters while preserving the caller's suspension identity.
            outcome.runtime = processed_runtime.model_copy(update={
                "phase": runtime.phase, "termination_reason": runtime.termination_reason,
            })
            outcome.processed_invocations.append(identity)
        except Exception as exc:  # noqa: BLE001 - one receipt cannot hide another's result
            outcome.errors[identity] = (
                exc.reason_code if isinstance(exc, ExecutionReceiptError) else type(exc).__name__
            )
            if identity not in outcome.pending_invocations:
                outcome.pending_invocations.append(identity)

    if charges:
        if not isinstance(baseline, dict):
            outcome.errors["accounting"] = "execution_accounting_baseline_missing"
        else:
            _project_accounting(outcome, baseline, charges)
    return outcome


def _project_accounting(
    outcome: ExecutionReconciliation, baseline: dict[str, Any],
    charges: dict[str, tuple[int, int, str]],
) -> None:
    """Idempotent projection from the first receipt's checkpoint basis."""
    counts = (baseline.get("actions_executed"), baseline.get("retry_tool_calls"))
    remaining = baseline.get("remaining_budget")
    agents = baseline.get("actions_by_agent")
    if (any(type(count) is not int or count < 0 for count in counts)
        or remaining is not None and (type(remaining) is not int or remaining < 0)
        or not isinstance(agents, dict)
        or any(type(value) is not int or value < 0 for value in agents.values())):
        outcome.errors["accounting"] = "execution_accounting_baseline_invalid"
        return
    base_actions, base_retries = cast(int, counts[0]), cast(int, counts[1])
    actions = base_actions + sum(executed for executed, _, _ in charges.values())
    retries = base_retries + sum(extra for _, extra, _ in charges.values())
    by_agent = dict(agents)
    for executed, _, agent in charges.values():
        by_agent[agent] = by_agent.get(agent, 0) + executed
    restored = max(outcome.runtime.actions_executed, actions)
    restored_retries = max(outcome.runtime.retry_tool_calls, retries)
    budget = None if remaining is None else max(
        0, base_actions + base_retries + remaining - restored - restored_retries,
    )
    if outcome.runtime.remaining_budget is not None:
        budget = (min(outcome.runtime.remaining_budget, budget)
                  if budget is not None else outcome.runtime.remaining_budget)
    outcome.runtime = outcome.runtime.model_copy(update={
        "actions_executed": restored, "retry_tool_calls": restored_retries,
        "remaining_budget": budget,
        "actions_by_agent": {
            agent: max(outcome.runtime.actions_by_agent.get(agent, 0), by_agent.get(agent, 0))
            for agent in outcome.runtime.actions_by_agent.keys() | by_agent.keys()
        },
    })
