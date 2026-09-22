"""OBSERVE — schedule an agent, project the graph into an observation."""

from __future__ import annotations

from redteam.core.contracts.events import EventType
from redteam.core.enums import AgentPhase
from redteam.orchestration.context import RuntimeDeps
from redteam.orchestration.nodes._support import advance, graph_service, publisher_for
from redteam.orchestration.progress import observation_fingerprint
from redteam.orchestration.state import GraphState, TerminationReason


async def observe_node(state: GraphState, *, deps: RuntimeDeps) -> GraphState:
    runtime = state["runtime"]
    settings = deps.settings

    service = graph_service(deps)
    await service.sweep_stale_predicates(current_step=runtime.step_count + 1)
    observation = await service.observe(
        step=runtime.step_count + 1,
        max_per_type=settings.observation_max_per_type,
        max_total=settings.observation_max_total,
    )
    fingerprint = observation_fingerprint(observation)
    no_progress_turns = (
        runtime.no_progress_turns + 1
        if runtime.last_progress_fingerprint == fingerprint
        else 0
    )

    agent = deps.agents.next_agent(
        current=runtime.current_agent,
        finished=runtime.finished_agents,
        observation=observation,
        phase=AgentPhase.OBSERVE,
        finished_revisions=runtime.agent_revision_cursors,
        rotate=runtime.step_count > 0,
    )
    if agent is None:
        # Nothing left to schedule — REPLAN will end the run and say why.
        no_agent_reason = (
            TerminationReason.NO_AGENTS
            if len(deps.agents) == 0
            else TerminationReason.NO_ACTIONABLE_PLAN
        )
        return advance(
            state,
            runtime.model_copy(
                update={
                    "phase": AgentPhase.REPLAN,
                    "termination_reason": runtime.termination_reason or no_agent_reason,
                    "last_progress_fingerprint": fingerprint,
                    "no_progress_turns": no_progress_turns,
                }
            ),
            observation=observation,
            plan_step=None,
        )

    publisher = publisher_for(state, deps, "orchestration.observe")
    last_event_id = state.get("last_event_id")
    started_agents = list(runtime.started_agents)

    if agent.agent_id not in started_agents:
        started = await publisher.publish(
            EventType.AGENT_STARTED,
            {"agent_id": agent.agent_id, "agent_name": agent.name, "step": runtime.step_count + 1},
            causation_id=last_event_id,
        )
        last_event_id = started.event_id
        started_agents.append(agent.agent_id)

    return advance(
        state,
        runtime.model_copy(
            update={
                "phase": AgentPhase.PLAN,
                "current_agent": agent.agent_id,
                "step_count": runtime.step_count + 1,
                "last_progress_fingerprint": fingerprint,
                "no_progress_turns": no_progress_turns,
                "finished_agents": [
                    item for item in runtime.finished_agents if item != agent.agent_id
                ],
                "started_agents": started_agents,
            }
        ),
        last_event_id=last_event_id,
        observation=observation,
        plan_step=None,
        action_proposal=None,
        policy_decision=None,
        tool_result=None,
        result_evidence_id=None,
        current_work_id=None,
    )
