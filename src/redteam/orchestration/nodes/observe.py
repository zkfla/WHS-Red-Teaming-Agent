"""OBSERVE — schedule the next Task (then its Agent), project the graph.

Scheduling is Task-first when the run has a Mission: OBSERVE picks the
highest-priority non-terminal Task whose Agent still has work, and the Agent
falls out of the Task. Without a Mission it falls back to Agent rotation, so
legacy/unit flows that never open a Mission are unchanged.
"""

from __future__ import annotations

from datetime import datetime, timezone

from redteam.attack_graph.observation import GraphObservation
from redteam.core.contracts.events import EventType
from redteam.core.enums import AgentPhase, TaskStatus
from redteam.core.ids import EventId, TaskId
from redteam.event_bus.publisher import EventPublisher
from redteam.orchestration.agents.base import AgentSpec
from redteam.orchestration.agents.registry import AgentRegistry
from redteam.orchestration.context import RuntimeDeps
from redteam.orchestration.nodes._support import advance, graph_service, publisher_for
from redteam.orchestration.progress import observation_fingerprint
from redteam.orchestration.state import GraphState, RuntimeState, TerminationReason


def _resolve_agent(agents: AgentRegistry, task: object) -> AgentSpec | None:
    """The agent a Task dispatches to: its explicit ``assigned_agent`` or the
    first schedulable role that owns its ``required_capability``."""
    assigned = getattr(task, "assigned_agent", None)
    if assigned is not None:
        return agents.get(assigned)
    capability = getattr(task, "required_capability", None)
    if capability:
        for definition in agents.definitions():
            if definition.schedulable and capability in definition.capabilities:
                return agents.get(definition.agent_id)
    return None


def _agent_exhausted(
    agents: AgentRegistry,
    agent: AgentSpec,
    runtime: RuntimeState,
    observation: GraphObservation,
    task_id: TaskId,
) -> bool:
    """True once an Agent has run its frontier dry for the current graph — the
    same revision-aware "finished" signal Agent rotation uses, reused here to
    decide a Task is done."""
    if runtime.active_task_id != task_id or agent.agent_id not in set(runtime.finished_agents):
        return False
    cursor = runtime.agent_revision_cursors.get(str(agent.agent_id))
    return cursor == agents.revision_fingerprint(agent.agent_id, observation)


def _task_agent(
    agents: AgentRegistry, task: object, observation: GraphObservation
) -> AgentSpec | None:
    agent = _resolve_agent(agents, task)
    if agent is None:
        return None
    definition = agents.definition(agent.agent_id)
    if definition is not None and not definition.is_active(observation, AgentPhase.OBSERVE):
        return None
    return agent


async def _select_task_first(
    deps: RuntimeDeps,
    runtime: RuntimeState,
    observation: GraphObservation,
    *,
    publisher: EventPublisher,
    last_event_id: EventId | None,
) -> tuple[AgentSpec | None, TaskId | None, EventId | None]:
    """Pick the next Task (highest priority first), then its Agent.

    Tasks whose Agent has exhausted its frontier are transitioned COMPLETED
    (Task Graph is the source of truth for progress); the first Task whose
    Agent still has work is started (READY -> RUNNING) and returned. When no
    Task can be scheduled the Agent is ``None`` and REPLAN ends the run.
    """
    store = deps.task_graph_store
    now = datetime.now(timezone.utc)
    tasks = await store.list_tasks(mission_id=runtime.mission_id)  # -priority order
    for task in tasks:
        if task.status not in (TaskStatus.READY, TaskStatus.RUNNING):
            continue
        agent = _resolve_agent(deps.agents, task)
        if agent is None:
            await store.transition_task(
                task.task_id, TaskStatus.BLOCKED, now=now,
                reason="no registered agent can perform this task",
            )
            event = await publisher.publish(
                EventType.TASK_BLOCKED,
                {"task_id": task.task_id, "reason": "no_registered_agent"},
                causation_id=last_event_id,
            )
            last_event_id = event.event_id
            continue
        if _task_agent(deps.agents, task, observation) is None:
            continue
        if _agent_exhausted(deps.agents, agent, runtime, observation, task.task_id):
            unmet = task.provenance.source.startswith("replan:") or (
                task.required and runtime.actions_by_agent.get(str(agent.agent_id), 0) == 0
            )
            outcome = TaskStatus.BLOCKED if unmet else TaskStatus.COMPLETED
            reason = "no candidate achieved the task objective" if unmet else None
            await store.transition_task(task.task_id, outcome, now=now, reason=reason)
            event = await publisher.publish(
                EventType.TASK_BLOCKED if unmet else EventType.TASK_COMPLETED,
                {"task_id": task.task_id, "agent_id": agent.agent_id,
                 **({"reason": reason} if reason else {})},
                causation_id=last_event_id,
            )
            last_event_id = event.event_id
            continue
        if task.status is TaskStatus.READY:
            await store.transition_task(task.task_id, TaskStatus.RUNNING, now=now)
            event = await publisher.publish(
                EventType.TASK_STARTED,
                {"task_id": task.task_id, "agent_id": agent.agent_id},
                causation_id=last_event_id,
            )
            last_event_id = event.event_id
        return agent, task.task_id, last_event_id
    return None, None, last_event_id


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

    publisher = publisher_for(state, deps, "orchestration.observe")
    last_event_id = state.get("last_event_id")

    current_task_id = runtime.active_task_id
    if runtime.mission_id is not None:
        agent, current_task_id, last_event_id = await _select_task_first(
            deps, runtime, observation, publisher=publisher, last_event_id=last_event_id
        )
    else:
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
                    "active_task_id": current_task_id,
                    "termination_reason": runtime.termination_reason or no_agent_reason,
                    "last_progress_fingerprint": fingerprint,
                    "no_progress_turns": no_progress_turns,
                }
            ),
            observation=observation,
            plan_step=None,
        )

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
                "active_task_id": current_task_id,
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
