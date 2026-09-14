"""POLICY node — hands the proposal to the Control Plane and nowhere else.

This is the structural enforcement of "Agent never calls Capability
directly": the only object this node produces that ``execute_node``
needs is a ``PolicyDecision``, and ``route_after_policy`` only sends the
graph to ``execute`` when that decision is ALLOW.
"""

from __future__ import annotations

from datetime import datetime, timezone

from redteam.core.contracts.events import EventEnvelope, EventType
from redteam.core.contracts.policy import ControlContext
from redteam.core.enums import AgentPhase, PolicyVerdict
from redteam.core.ids import EventId, new_id
from redteam.orchestration.context import RuntimeDeps
from redteam.orchestration.state import GraphState


async def policy_node(state: GraphState, *, deps: RuntimeDeps) -> GraphState:
    runtime = state["runtime"]
    proposal = state["action_proposal"]
    assert proposal is not None

    context = ControlContext(engagement_id=runtime.engagement_id, agent_id=runtime.current_agent)
    decision = await deps.control_plane.evaluate(proposal, context)

    await deps.event_bus.publish(
        EventEnvelope(
            event_id=EventId(new_id("evt")),
            event_type=EventType.POLICY_EVALUATED,
            run_id=runtime.run_id,
            engagement_id=runtime.engagement_id,
            source="orchestration.policy",
            timestamp=datetime.now(timezone.utc),
            payload={"action_id": decision.action_id, "verdict": decision.verdict.value},
        )
    )

    if decision.verdict == PolicyVerdict.ALLOW:
        event_type = EventType.ACTION_APPROVED
        next_phase = AgentPhase.EXECUTE
    elif decision.verdict == PolicyVerdict.REQUIRE_APPROVAL:
        event_type = EventType.APPROVAL_REQUIRED
        next_phase = AgentPhase.REPLAN
    else:
        event_type = EventType.ACTION_DENIED
        next_phase = AgentPhase.REPLAN

    await deps.event_bus.publish(
        EventEnvelope(
            event_id=EventId(new_id("evt")),
            event_type=event_type,
            run_id=runtime.run_id,
            engagement_id=runtime.engagement_id,
            source="orchestration.policy",
            timestamp=datetime.now(timezone.utc),
            payload={"action_id": decision.action_id, "reasons": decision.reasons},
        )
    )

    new_runtime = runtime.model_copy(update={"phase": next_phase})
    return {**state, "runtime": new_runtime, "policy_decision": decision}
