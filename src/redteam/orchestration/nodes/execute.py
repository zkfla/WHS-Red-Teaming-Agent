"""EXECUTE — only reachable after an ALLOW ``PolicyDecision``.

Runs the tool the approved ``PlanStep`` declared, and owns execution
accounting (budget, consecutive failures).
"""

from __future__ import annotations

from redteam.core.contracts.events import EventType
from redteam.core.convert import build_tool_invocation
from redteam.core.enums import AgentPhase
from redteam.orchestration.context import RuntimeDeps
from redteam.orchestration.nodes._support import advance, publisher_for
from redteam.orchestration.state import GraphState

DEFAULT_TIMEOUT_SECONDS = 10.0


async def execute_node(state: GraphState, *, deps: RuntimeDeps) -> GraphState:
    runtime = state["runtime"]
    proposal = state["action_proposal"]
    decision = state["policy_decision"]
    step = state.get("plan_step")
    assert proposal is not None and decision is not None and step is not None

    correlation_id = str(proposal.action_id)
    publisher = publisher_for(state, deps, "orchestration.execute")

    invocation = build_tool_invocation(
        proposal,
        decision,
        tool_id=step.tool_id,
        timeout_seconds=DEFAULT_TIMEOUT_SECONDS,
        correlation_id=correlation_id,
    )

    started = await publisher.publish(
        EventType.TOOL_EXECUTION_STARTED,
        {
            "invocation_id": invocation.invocation_id,
            "action_id": invocation.action_id,
            "tool_id": invocation.tool_id,
        },
        correlation_id=correlation_id,
        causation_id=state.get("last_event_id"),
    )

    result = await deps.tool_gateway.invoke(invocation)

    finished = await publisher.publish(
        EventType.TOOL_EXECUTION_FINISHED if result.success else EventType.TOOL_EXECUTION_FAILED,
        {
            "invocation_id": result.invocation_id,
            "action_id": result.action_id,
            "success": result.success,
            "error_code": result.error.code if result.error else None,
        },
        correlation_id=correlation_id,
        causation_id=started.event_id,
    )

    remaining_budget = (
        None if runtime.remaining_budget is None else max(runtime.remaining_budget - 1, 0)
    )

    return advance(
        state,
        runtime.model_copy(
            update={
                "phase": AgentPhase.VERIFY,
                "actions_executed": runtime.actions_executed + 1,
                "consecutive_failures": 0 if result.success else runtime.consecutive_failures + 1,
                "remaining_budget": remaining_budget,
            }
        ),
        last_event_id=finished.event_id,
        tool_result=result,
    )
