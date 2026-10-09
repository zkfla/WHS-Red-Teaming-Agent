"""Isolated action worker used by the LangGraph ``Send`` fan-out path."""

from __future__ import annotations

from contextlib import suppress
from datetime import datetime, timedelta, timezone

from redteam.core.contracts.tool import EXECUTION_STARTED_METADATA_KEY
from redteam.core.enums import AgentPhase, PolicyVerdict
from redteam.core.redaction import redact_sensitive_text
from redteam.orchestration.analysis_budget import verification_budget
from redteam.orchestration.context import RuntimeDeps
from redteam.orchestration.diagnostics import operational_exception_diagnostic
from redteam.orchestration.dispatch import WorkerOutcome
from redteam.orchestration.execution_journal import preserved_result
from redteam.orchestration.nodes._support import graph_service
from redteam.orchestration.nodes.execute import execute_node
from redteam.orchestration.nodes.policy import denial_severity, policy_node
from redteam.orchestration.nodes.update import update_node
from redteam.orchestration.nodes.verify import verify_node
from redteam.orchestration.runtime_observation import RuntimeObservation
from redteam.orchestration.state import GraphState
from redteam.orchestration.work.models import WorkStatus


def _own_observations(base, local_runtime, action_id):  # noqa: ANN001, ANN202
    """Collect only records this worker wrote for its own action."""

    carried = []
    for observation in local_runtime.recent_observations:
        if observation.action_id != action_id:
            continue
        if observation in base:
            continue
        carried.append(observation)
    return carried


async def worker_node(state: GraphState, *, deps: RuntimeDeps) -> GraphState:
    """Run one leased action without writing shared coordinator channels.

    Control-plane admission is serialized because its budget and rate-limit
    counters are mutable. Once ALLOW is returned, different tool calls execute
    concurrently in separate local state dictionaries.
    """

    runtime = state["runtime"]
    item = state["dispatch_item"]
    action_id = str(item.proposal.action_id)
    base_observations = list(runtime.recent_observations)
    local: GraphState = {
        "runtime": runtime.model_copy(
            deep=True,
            update={
                "current_agent": item.proposal.agent_id,
                "active_path_step_id": item.plan_step.path_step_id,
                # This counter describes the current action, unlike the
                # cumulative retry_tool_calls. Failures before EXECUTE
                # returns must not inherit a previous action's retries.
                "retry_count": 0,
            },
        ),
        "observation": None,
        "plan_step": item.plan_step,
        "action_proposal": item.proposal,
        "policy_decision": None,
        "tool_result": None,
        "result_evidence_id": None,
        "current_work_id": item.work_id,
        "dispatch_items": [],
        "worker_outcomes": {},
        "last_event_id": item.last_event_id,
    }
    # Private to this actual action, including all analysis retries. Keep it
    # outside checkpoint/model data and retain it if a later node fails.
    model_budget = verification_budget(local, deps=deps)

    work = await deps.work_store.get(item.work_id)
    if (
        work is None
        or work.instance_id != runtime.instance_id
        or item.proposal.instance_id != runtime.instance_id
    ):
        await deps.work_store.release(
            item.work_id, owner=runtime.work_owner, now=datetime.now(timezone.utc)
        )
        return {
            "worker_outcomes": {
                action_id: WorkerOutcome(
                    action_id=action_id,
                    work_id=item.work_id,
                    delegation_id=item.plan_step.delegation_id,
                    candidate_key=item.plan_step.candidate_key,
                    agent_id=str(item.proposal.agent_id),
                    error="instance_work_mismatch",
                )
            }
        }

    heartbeat = await deps.work_store.heartbeat(
        item.work_id,
        owner=runtime.work_owner,
        now=datetime.now(timezone.utc),
        ttl=timedelta(seconds=deps.settings.lease_ttl_seconds),
    )
    if heartbeat is None:
        return {
            "worker_outcomes": {
                action_id: WorkerOutcome(
                    action_id=action_id,
                    work_id=item.work_id,
                    delegation_id=item.plan_step.delegation_id,
                    candidate_key=item.plan_step.candidate_key,
                    agent_id=str(item.proposal.agent_id),
                    error="lease_lost_before_execution",
                )
            }
        }

    try:
        async with deps.policy_lock:
            if runtime.work_owner in deps.batch_pending_approvals:
                # This sibling never entered Control or Capability. Preserve
                # its original Work row for a later turn after the held
                # approval is resolved; do not create a second Approval that
                # the single-action durable checkpoint cannot represent.
                released = await deps.work_store.release(
                    item.work_id, owner=runtime.work_owner, now=datetime.now(timezone.utc)
                )
                return {
                    "worker_outcomes": {
                        action_id: WorkerOutcome(
                            action_id=action_id,
                            work_id=item.work_id,
                    delegation_id=item.plan_step.delegation_id,
                    candidate_key=item.plan_step.candidate_key,
                    agent_id=str(item.proposal.agent_id),
                            error=("deferred_for_approval" if released else "deferred_lease_lost"),
                        )
                    }
                }
            local = await policy_node(local, deps=deps)
            decision = local.get("policy_decision")
            if decision is not None and decision.verdict is PolicyVerdict.REQUIRE_APPROVAL:
                deps.batch_pending_approvals.add(runtime.work_owner)
        decision = local.get("policy_decision")

        if local["runtime"].phase is AgentPhase.EXECUTE:
            local = await execute_node(local, deps=deps)
            local = await verify_node(local, deps=deps, budget=model_budget)
        local = await update_node(local, deps=deps)

        result = local.get("tool_result")
        decision = local.get("policy_decision")
        # EXECUTE owns admission and execution accounting. A returned result
        # can also describe a rejection before the provider started.
        executed = local["runtime"].actions_executed > runtime.actions_executed
        outcome = WorkerOutcome(
            action_id=action_id,
            model_tool_call_id=str(getattr(item.plan_step, "model_tool_call_id", "") or ""),
            work_id=item.work_id,
            delegation_id=item.plan_step.delegation_id,
            candidate_key=item.plan_step.candidate_key,
            agent_id=str(item.proposal.agent_id),
            evidence_ids=(
                [str(local.get("result_evidence_id"))]
                if local.get("result_evidence_id") is not None
                else []
            ),
            executed=executed,
            retry_attempts=local["runtime"].retry_count if executed else 0,
            model_decision_attempts=model_budget.used,
            model_known_tokens_used=model_budget.tokens_used,
            model_usage_unknown=model_budget.unknown_usage_count,
            model_tokens_used=(
                model_budget.tokens_used if model_budget.tokens_fully_known else None
            ),
            succeeded=result.success if result is not None else None,
            policy_verdict=decision.verdict if decision is not None else None,
            policy_severity=denial_severity(decision) if decision is not None else None,
            policy_reason=(
                redact_sensitive_text(decision.reasons[0])[:200]
                if decision is not None and decision.reasons
                else None
            ),
            policy_budget_abort=bool(decision and "budget" in decision.matched_rules),
            approval_id=decision.approval_id if decision is not None else None,
            verdict=local["runtime"].last_verdict,
            last_event_id=local.get("last_event_id"),
            runtime_observations=_own_observations(base_observations, local["runtime"], action_id),
        )
    except Exception as exc:  # noqa: BLE001 - isolate one worker from its siblings
        result = local.get("tool_result")
        receipt_error = None
        if result is None:
            try:
                original = await preserved_result(
                    deps.evidence_store, run_id=runtime.run_id,
                    action_id=item.proposal.action_id, instance_id=runtime.instance_id,
                    work_id=item.work_id,
                )
            except Exception as receipt_exc:  # noqa: BLE001 - preserve sibling isolation
                original = None
                receipt_error = type(receipt_exc).__name__
            if original is not None:
                result, receipt_id = original
                local["tool_result"] = result
                local["result_evidence_id"] = receipt_id
        if item.proposal.hypothesis_id is not None:
            with suppress(Exception):
                service = graph_service(deps)
                hypothesis = await service.get_hypothesis(str(item.proposal.hypothesis_id))
                if hypothesis is not None and hypothesis.status == "open":
                    await service.resolve_hypothesis(
                        str(item.proposal.hypothesis_id),
                        status="inconclusive",
                        step=runtime.step_count,
                    )
        await deps.work_store.complete(
            item.work_id,
            owner=runtime.work_owner,
            status=WorkStatus.FAILED,
            now=datetime.now(timezone.utc),
            reason=f"worker_error:{type(exc).__name__}",
        )
        decision = local.get("policy_decision")
        attempted = (
            result.metadata.get(EXECUTION_STARTED_METADATA_KEY) is not False
            if result is not None
            else decision is not None and decision.verdict is PolicyVerdict.ALLOW
        )
        retry_attempts = (
            int(result.metadata.get("retry_attempts", 0)) if result is not None
            else local["runtime"].retry_count
        )
        outcome = WorkerOutcome(
            action_id=action_id,
            model_tool_call_id=str(getattr(item.plan_step, "model_tool_call_id", "") or ""),
            work_id=item.work_id,
            delegation_id=item.plan_step.delegation_id,
            candidate_key=item.plan_step.candidate_key,
            agent_id=str(item.proposal.agent_id),
            evidence_ids=(
                [str(local.get("result_evidence_id"))]
                if local.get("result_evidence_id") is not None
                else []
            ),
            executed=attempted,
            retry_attempts=retry_attempts if attempted else 0,
            model_decision_attempts=model_budget.used,
            model_known_tokens_used=model_budget.tokens_used,
            model_usage_unknown=model_budget.unknown_usage_count,
            model_tokens_used=(
                model_budget.tokens_used if model_budget.tokens_fully_known else None
            ),
            succeeded=result.success if result is not None else False if attempted else None,
            policy_verdict=decision.verdict if decision is not None else None,
            policy_severity=denial_severity(decision) if decision is not None else None,
            policy_reason=(
                redact_sensitive_text(decision.reasons[0])[:200]
                if decision is not None and decision.reasons
                else None
            ),
            policy_budget_abort=bool(decision and "budget" in decision.matched_rules),
            approval_id=decision.approval_id if decision is not None else None,
            verdict=local["runtime"].last_verdict,
            last_event_id=local.get("last_event_id"),
            runtime_observations=[
                *_own_observations(base_observations, local["runtime"], action_id),
                RuntimeObservation(
                    kind="state",
                    summary=f"Worker {action_id} failed: {type(exc).__name__}"[:500],
                    outcome="failed",
                    step=runtime.step_count,
                    action_id=action_id,
                    diagnostic=operational_exception_diagnostic(
                        exc,
                        stage="worker",
                        code="worker_error",
                        summary="isolated worker failed",
                    ),
                    alternative_allowed=True,
                ),
            ],
            # Checkpoint only the class. Exception text may contain tool output,
            # target secrets, or transport details and belongs in protected logs.
            error=(f"execution_receipt_unavailable:{receipt_error}"
                   if receipt_error is not None else type(exc).__name__),
        )

    return {"worker_outcomes": {action_id: outcome}}
