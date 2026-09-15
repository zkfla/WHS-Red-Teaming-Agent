"""OBSERVE — schedule an agent, project the graph into an observation."""

from __future__ import annotations

from redteam.core.contracts.events import EventType
from redteam.core.enums import AgentPhase
from redteam.orchestration.context import RuntimeDeps
from redteam.orchestration.nodes._support import advance, graph_service, publisher_for
from redteam.orchestration.state import GraphState, TerminationReason


async def observe_node(state: GraphState, *, deps: RuntimeDeps) -> GraphState:
    runtime = state["runtime"]
    settings = deps.settings

    agent = deps.agents.next_agent(
        current=runtime.current_agent, finished=runtime.finished_agents
    )
    if agent is None:
        # Nothing left to schedule — REPLAN will end the run and say why.
        return advance(
            state,
            runtime.model_copy(
                update={
                    "phase": AgentPhase.REPLAN,
                    "termination_reason": runtime.termination_reason
                    or TerminationReason.NO_AGENTS,
                }
            ),
            observation=None,
            plan_step=None,
        )

    publisher = publisher_for(state, deps, "orchestration.observe")
    last_event_id = state.get("last_event_id")

    if runtime.current_agent != agent.agent_id:
        started = await publisher.publish(
            EventType.AGENT_STARTED,
            {"agent_id": agent.agent_id, "agent_name": agent.name, "step": runtime.step_count + 1},
        )
        last_event_id = started.event_id

    observation = await graph_service(deps).observe(
        step=runtime.step_count + 1,
        max_per_type=settings.observation_max_per_type,
        max_total=settings.observation_max_total,
    )

    return advance(
        state,
        runtime.model_copy(
            update={
                "phase": AgentPhase.PLAN,
                "current_agent": agent.agent_id,
                "step_count": runtime.step_count + 1,
            }
        ),
        last_event_id=last_event_id,
        observation=observation,
        plan_step=None,
        action_proposal=None,
        policy_decision=None,
        tool_result=None,
    )
