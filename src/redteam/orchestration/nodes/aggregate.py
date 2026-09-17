"""Reduce one parallel dispatch batch into the durable runtime counters."""

from __future__ import annotations

from langgraph.types import Overwrite

from redteam.core.enums import AgentPhase
from redteam.orchestration.context import RuntimeDeps
from redteam.orchestration.nodes._support import advance
from redteam.orchestration.state import GraphState


async def aggregate_node(state: GraphState, *, deps: RuntimeDeps) -> GraphState:
    del deps  # the reducer is pure; dependency kept for the stable node signature
    runtime = state["runtime"]
    items = state.get("dispatch_items", [])
    all_outcomes = state.get("worker_outcomes", {})
    outcomes = [
        all_outcomes[str(item.proposal.action_id)]
        for item in items
        if str(item.proposal.action_id) in all_outcomes
    ]

    executed = [item for item in outcomes if item.executed]
    succeeded = [item for item in executed if item.succeeded]
    failed = [item for item in executed if item.succeeded is False]
    remaining_budget = runtime.remaining_budget
    if remaining_budget is not None:
        remaining_budget = max(remaining_budget - len(executed), 0)

    consecutive_failures = runtime.consecutive_failures
    if succeeded:
        consecutive_failures = 0
    elif failed:
        consecutive_failures += len(failed)

    executed_ids = {item.action_id for item in executed}
    actions_by_agent = dict(runtime.actions_by_agent)
    for dispatch in items:
        if str(dispatch.proposal.action_id) not in executed_ids:
            continue
        agent_key = str(dispatch.proposal.agent_id)
        actions_by_agent[agent_key] = actions_by_agent.get(agent_key, 0) + 1

    last_with_verdict = next(
        (item for item in reversed(outcomes) if item.verdict is not None), None
    )
    last_with_event = next(
        (item for item in reversed(outcomes) if item.last_event_id is not None), None
    )

    return advance(
        state,
        runtime.model_copy(
            update={
                "phase": AgentPhase.REPLAN,
                "actions_executed": runtime.actions_executed + len(executed),
                "consecutive_failures": consecutive_failures,
                "remaining_budget": remaining_budget,
                "actions_by_agent": actions_by_agent,
                "last_verdict": (
                    last_with_verdict.verdict if last_with_verdict else runtime.last_verdict
                ),
                "pending_action_id": None,
                "active_hypothesis_id": None,
            }
        ),
        last_event_id=last_with_event.last_event_id if last_with_event else None,
        dispatch_items=[],
        # ``worker_outcomes`` has a merge reducer so parallel workers can write
        # independently.  A plain ``{}`` would be merged as a no-op and retain
        # every prior turn in checkpoints.  The coordinator has folded this
        # batch into durable counters, so replace the channel explicitly.
        worker_outcomes=Overwrite(value={}),
        current_work_id=None,
    )
