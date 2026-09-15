"""REPLAN — continue the loop, or stop with a named reason."""

from __future__ import annotations

from redteam.core.contracts.events import EventType
from redteam.core.enums import AgentPhase
from redteam.orchestration.context import RuntimeDeps
from redteam.orchestration.nodes._support import advance, publisher_for
from redteam.orchestration.state import GraphState, RuntimeState, TerminationReason


def _hard_stop(runtime: RuntimeState, deps: RuntimeDeps) -> str | None:
    settings = deps.settings
    if runtime.step_count >= settings.max_steps:
        return TerminationReason.MAX_STEPS_REACHED
    if runtime.remaining_budget is not None and runtime.remaining_budget <= 0:
        return TerminationReason.BUDGET_EXHAUSTED
    if runtime.consecutive_failures >= settings.max_consecutive_failures:
        return TerminationReason.CONSECUTIVE_FAILURES
    return None


async def replan_node(state: GraphState, *, deps: RuntimeDeps) -> GraphState:
    runtime = state["runtime"]
    publisher = publisher_for(state, deps, "orchestration.replan")
    last_event_id = state.get("last_event_id")

    agent_id = runtime.current_agent
    finished_agents = list(runtime.finished_agents)
    # No proposal came out of PLAN: this agent has nothing left to offer.
    agent_exhausted = agent_id is not None and state.get("action_proposal") is None

    if agent_exhausted and agent_id is not None and agent_id not in finished_agents:
        finished_agents.append(agent_id)
        spec = deps.agents.get(agent_id)
        retired = await publisher.publish(
            EventType.AGENT_FINISHED,
            {
                "agent_id": agent_id,
                "agent_name": spec.name if spec else None,
                "actions_executed": runtime.actions_executed,
                "reason": "no further steps available",
            },
            causation_id=last_event_id,
        )
        last_event_id = retired.event_id

    reason = runtime.termination_reason or _hard_stop(runtime, deps)
    if reason is None:
        next_agent = deps.agents.next_agent(current=agent_id, finished=finished_agents)
        if next_agent is None:
            reason = TerminationReason.NO_ACTIONABLE_PLAN

    if reason is not None:
        return advance(
            state,
            runtime.model_copy(
                update={
                    "phase": AgentPhase.FINISHED,
                    "termination_reason": reason,
                    "finished_agents": finished_agents,
                }
            ),
            last_event_id=last_event_id,
            action_proposal=None,
            policy_decision=None,
            tool_result=None,
            plan_step=None,
        )

    return advance(
        state,
        runtime.model_copy(
            update={
                "phase": AgentPhase.OBSERVE,
                "finished_agents": finished_agents,
                "pending_action_id": None,
                "active_hypothesis_id": None,
            }
        ),
        last_event_id=last_event_id,
        action_proposal=None,
        policy_decision=None,
        tool_result=None,
        plan_step=None,
    )
