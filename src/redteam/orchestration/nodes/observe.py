"""Observe data and operator instructions; strategy is decided in PLAN."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from redteam.attack_graph.observation import GraphObservation
from redteam.core.contracts.events import EventType
from redteam.core.enums import AgentPhase, TaskStatus
from redteam.orchestration.context import RuntimeDeps
from redteam.orchestration.knowledge import project_knowledge
from redteam.orchestration.nodes._support import advance, graph_service, publisher_for
from redteam.orchestration.operator_intents import OperatorIntentService
from redteam.orchestration.progress import knowledge_fingerprint
from redteam.orchestration.state import GraphState, TerminationReason


def _task_agent(agents, task, observation: GraphObservation):
    assigned = getattr(task, "assigned_agent", None)
    return agents.get(assigned) if assigned else next(iter(agents.all()), None)


async def observe_node(state: GraphState, *, deps: RuntimeDeps) -> GraphState:
    runtime = state["runtime"]
    service = graph_service(deps)
    await service.sweep_stale_predicates(current_step=runtime.step_count + 1)
    observation = await service.observe(
        step=runtime.step_count + 1,
        max_per_type=deps.settings.observation_max_per_type,
        max_total=deps.settings.observation_max_total,
    )
    publisher = publisher_for(state, deps, "orchestration.observe")
    last_event_id = state.get("last_event_id")
    intents = OperatorIntentService.from_deps(deps)
    if intents is not None and runtime.mission_id is not None:
        drain = await intents.process_pending(
            mission_id=runtime.mission_id,
            run_id=runtime.run_id,
            publisher=publisher,
            last_event_id=last_event_id,
        )
        last_event_id = drain.last_event_id
        runtime = runtime.model_copy(
            update={
                "operator_intent_id": drain.last_intent_id or runtime.operator_intent_id,
                "mission_status": drain.mission_status or runtime.mission_status,
            }
        )
        if drain.paused or drain.stopped:
            return advance(
                state,
                runtime.model_copy(
                    update={
                        "phase": AgentPhase.REPLAN,
                        "current_agent": None,
                        "termination_reason": TerminationReason.OPERATOR_STOPPED
                        if drain.stopped
                        else TerminationReason.OPERATOR_PAUSED,
                    }
                ),
                last_event_id=last_event_id,
                observation=observation,
                plan_step=None,
                action_proposal=None,
                dispatch_items=[],
            )
    knowledge = await project_knowledge(
        service,
        deps.evidence_store,
        str(runtime.run_id),
        step=runtime.step_count + 1,
    )
    fingerprint = knowledge_fingerprint(knowledge)
    path = await deps.path_store.get_active(str(runtime.run_id))
    completed = (
        [
            (item.step_id, item.objective, item.outcome)
            for item in path.steps
            if item.status == "completed" and item.evidence_ids
        ]
        if path
        else []
    )
    fingerprint = hashlib.sha256(json.dumps([fingerprint, sorted(completed)]).encode()).hexdigest()
    no_progress = (
        runtime.no_progress_turns + 1 if runtime.last_progress_fingerprint == fingerprint else 0
    )
    task_id = runtime.active_task_id
    if runtime.mission_id is not None:
        from redteam.orchestration.operator_intents.service import _is_suppressed

        mission = await deps.task_graph_store.get_mission(runtime.mission_id)
        tasks = await deps.task_graph_store.list_tasks(mission_id=runtime.mission_id)
        # Dependency readiness is bookkeeping, not a strategy decision. Only
        # release tasks already registered by the operator/planning layer.
        by_id = {item.task_id: item for item in tasks}
        for item in tasks:
            if item.status not in {TaskStatus.PENDING, TaskStatus.BLOCKED}:
                continue
            if mission is not None and _is_suppressed(item, mission):
                continue
            if not item.dependency_task_ids:
                continue
            if not all(
                identity in by_id and by_id[identity].status is TaskStatus.COMPLETED
                for identity in item.dependency_task_ids
            ):
                continue
            if item.status is TaskStatus.PENDING:
                await deps.task_graph_store.transition_task(
                    item.task_id, TaskStatus.READY, now=datetime.now(timezone.utc)
                )
            elif item.blocking_reason == "required dependency is blocked":
                await deps.task_graph_store.unblock_task(
                    item.task_id, now=datetime.now(timezone.utc)
                )
                event = await publisher.publish(
                    EventType.TASK_UNBLOCKED,
                    {"task_id": item.task_id, "reason": "dependencies_completed"},
                    causation_id=last_event_id,
                )
                last_event_id = event.event_id
        tasks = await deps.task_graph_store.list_tasks(mission_id=runtime.mission_id)
        task = next(
            (
                item
                for item in tasks
                if item.status in {TaskStatus.READY, TaskStatus.RUNNING}
                and (mission is None or not _is_suppressed(item, mission))
            ),
            None,
        )
        task_id = task.task_id if task else None
        if task is not None:
            task_id = task.task_id
            if task.status is TaskStatus.READY:
                await deps.task_graph_store.transition_task(
                    task.task_id,
                    TaskStatus.RUNNING,
                    now=datetime.now(timezone.utc),
                )
                event = await publisher.publish(
                    EventType.TASK_STARTED,
                    {"mission_id": runtime.mission_id, "task_id": task.task_id},
                    causation_id=last_event_id,
                )
                last_event_id = event.event_id
    # This selects the coordinator identity, not a tool, strategy or first scan.
    agent = deps.agents.get(runtime.current_agent) if runtime.current_agent else None
    agent = agent or next(iter(deps.agents.all()), None)
    if agent is None:
        return advance(
            state,
            runtime.model_copy(
                update={
                    "phase": AgentPhase.REPLAN,
                    "termination_reason": TerminationReason.NO_AGENTS,
                }
            ),
            observation=observation,
            plan_step=None,
            action_proposal=None,
        )
    started_agents = list(runtime.started_agents)
    if agent.agent_id not in started_agents:
        event = await publisher.publish(
            EventType.AGENT_STARTED,
            {
                "agent_id": agent.agent_id,
                "agent_name": agent.name,
                "step": runtime.step_count + 1,
            },
            causation_id=last_event_id,
        )
        last_event_id = event.event_id
        started_agents.append(agent.agent_id)
    return advance(
        state,
        runtime.model_copy(
            update={
                "phase": AgentPhase.PLAN,
                "current_agent": agent.agent_id,
                "active_task_id": task_id,
                "step_count": runtime.step_count + 1,
                "last_progress_fingerprint": fingerprint,
                "no_progress_turns": no_progress,
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
        dispatch_items=[],
    )
