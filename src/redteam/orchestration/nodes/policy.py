"""POLICY — hand the proposal to the Control Plane and nowhere else.

Makes no judgement of its own, including on REQUIRE_APPROVAL, which
routes away rather than waiting.
"""

from __future__ import annotations

from datetime import datetime, timezone

from redteam.core.contracts.events import EventType
from redteam.core.contracts.policy import ControlContext, PolicyDecision
from redteam.core.enums import AgentPhase, PolicyVerdict
from redteam.orchestration.context import RuntimeDeps
from redteam.orchestration.nodes._support import advance, publisher_for
from redteam.orchestration.state import GraphState, TerminationReason

_OUTCOME = {
    PolicyVerdict.ALLOW: (EventType.ACTION_APPROVED, AgentPhase.EXECUTE),
    PolicyVerdict.REQUIRE_APPROVAL: (EventType.APPROVAL_REQUIRED, AgentPhase.REPLAN),
    PolicyVerdict.DENY: (EventType.ACTION_DENIED, AgentPhase.REPLAN),
}


async def policy_node(state: GraphState, *, deps: RuntimeDeps) -> GraphState:
    runtime = state["runtime"]
    proposal = state["action_proposal"]
    assert proposal is not None, "policy_node runs after a proposal was produced"

    correlation_id = str(proposal.action_id)
    publisher = publisher_for(state, deps, "orchestration.policy")

    context = ControlContext(
        engagement_id=runtime.engagement_id,
        agent_id=runtime.current_agent,
        correlation_id=correlation_id,
    )
    try:
        decision = await deps.control_plane.evaluate(proposal, context)
    except Exception as exc:  # noqa: BLE001 - policy failures must fail closed
        decision = PolicyDecision(
            action_id=proposal.action_id,
            verdict=PolicyVerdict.DENY,
            reasons=["control plane evaluation failed"],
            matched_rules=[f"control_error:{type(exc).__name__}"],
            evaluated_at=datetime.now(timezone.utc),
        )

    evaluated = await publisher.publish(
        EventType.POLICY_EVALUATED,
        {
            "action_id": decision.action_id,
            "verdict": decision.verdict.value,
            "matched_rules": decision.matched_rules,
            "budget_remaining": decision.budget_remaining,
        },
        correlation_id=correlation_id,
        causation_id=state.get("last_event_id"),
    )

    event_type, next_phase = _OUTCOME[decision.verdict]
    outcome = await publisher.publish(
        event_type,
        {
            "action_id": decision.action_id,
            "reasons": decision.reasons,
            "approval_id": decision.approval_id,
        },
        correlation_id=correlation_id,
        causation_id=evaluated.event_id,
    )

    runtime_update: dict[str, object] = {"phase": next_phase}
    if decision.verdict is PolicyVerdict.REQUIRE_APPROVAL:
        runtime_update.update(
            {
                "termination_reason": TerminationReason.APPROVAL_PENDING,
                "pending_approval_id": decision.approval_id,
            }
        )

    return advance(
        state,
        runtime.model_copy(update=runtime_update),
        last_event_id=outcome.event_id,
        policy_decision=decision,
    )
