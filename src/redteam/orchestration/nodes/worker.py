"""Isolated action worker used by the LangGraph ``Send`` fan-out path."""

from __future__ import annotations

from contextlib import suppress
from datetime import datetime, timedelta, timezone

from redteam.core.enums import AgentPhase, PolicyVerdict
from redteam.orchestration.context import RuntimeDeps
from redteam.orchestration.dispatch import WorkerOutcome
from redteam.orchestration.nodes._support import graph_service
from redteam.orchestration.nodes.execute import execute_node
from redteam.orchestration.nodes.policy import policy_node
from redteam.orchestration.nodes.update import update_node
from redteam.orchestration.nodes.verify import verify_node
from redteam.orchestration.state import GraphState
from redteam.orchestration.work.models import WorkStatus


async def worker_node(state: GraphState, *, deps: RuntimeDeps) -> GraphState:
    """Run one leased action without writing shared coordinator channels.

    Control-plane admission is serialized because its budget and rate-limit
    counters are mutable. Once ALLOW is returned, different tool calls execute
    concurrently in separate local state dictionaries.
    """

    runtime = state["runtime"]
    item = state["dispatch_item"]
    action_id = str(item.proposal.action_id)
    local: GraphState = {
        "runtime": runtime.model_copy(deep=True),
        "observation": None,
        "plan_step": item.plan_step,
        "action_proposal": item.proposal,
        "policy_decision": None,
        "tool_result": None,
        "current_work_id": item.work_id,
        "dispatch_items": [],
        "worker_outcomes": {},
        "last_event_id": item.last_event_id,
    }

    heartbeat = await deps.work_store.heartbeat(
        item.work_id,
        owner=str(runtime.run_id),
        now=datetime.now(timezone.utc),
        ttl=timedelta(seconds=deps.settings.lease_ttl_seconds),
    )
    if heartbeat is None:
        return {
            "worker_outcomes": {
                action_id: WorkerOutcome(
                    action_id=action_id,
                    work_id=item.work_id,
                    error="lease_lost_before_execution",
                )
            }
        }

    try:
        async with deps.policy_lock:
            local = await policy_node(local, deps=deps)
        decision = local.get("policy_decision")

        if local["runtime"].phase is AgentPhase.EXECUTE:
            local = await execute_node(local, deps=deps)
            local = await verify_node(local, deps=deps)
        local = await update_node(local, deps=deps)

        result = local.get("tool_result")
        outcome = WorkerOutcome(
            action_id=action_id,
            work_id=item.work_id,
            executed=result is not None,
            succeeded=result.success if result is not None else None,
            policy_verdict=decision.verdict if decision is not None else None,
            approval_id=decision.approval_id if decision is not None else None,
            verdict=local["runtime"].last_verdict,
            last_event_id=local.get("last_event_id"),
        )
    except Exception as exc:  # noqa: BLE001 - isolate one worker from its siblings
        result = local.get("tool_result")
        if item.proposal.hypothesis_id is not None:
            with suppress(Exception):
                service = graph_service(deps)
                hypothesis = await service.get_hypothesis(
                    str(item.proposal.hypothesis_id)
                )
                if hypothesis is not None and hypothesis.status == "open":
                    await service.resolve_hypothesis(
                        str(item.proposal.hypothesis_id),
                        status="inconclusive",
                        step=runtime.step_count,
                    )
        await deps.work_store.complete(
            item.work_id,
            owner=str(runtime.run_id),
            status=WorkStatus.FAILED,
            now=datetime.now(timezone.utc),
            reason=f"worker_error:{type(exc).__name__}",
        )
        decision = local.get("policy_decision")
        attempted = decision is not None and decision.verdict is PolicyVerdict.ALLOW
        outcome = WorkerOutcome(
            action_id=action_id,
            work_id=item.work_id,
            executed=attempted,
            succeeded=result.success if result is not None else False if attempted else None,
            policy_verdict=decision.verdict if decision is not None else None,
            approval_id=decision.approval_id if decision is not None else None,
            verdict=local["runtime"].last_verdict,
            last_event_id=local.get("last_event_id"),
            # Checkpoint only the class. Exception text may contain tool output,
            # target secrets, or transport details and belongs in protected logs.
            error=type(exc).__name__,
        )

    return {"worker_outcomes": {action_id: outcome}}
