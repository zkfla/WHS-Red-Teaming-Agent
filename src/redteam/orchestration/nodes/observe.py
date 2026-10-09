"""Observe data and operator instructions; strategy is decided in PLAN."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from redteam.attack_graph.observation import GraphObservation
from redteam.core.contracts.events import EventType
from redteam.core.enums import AgentPhase, TaskStatus
from redteam.orchestration.context import RuntimeDeps
from redteam.orchestration.nodes._support import advance, graph_service, publisher_for
from redteam.orchestration.operator_intents import OperatorIntentService
from redteam.orchestration.progress import canonical_progress_fingerprint
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
        extra_content_keys=tuple(
            sorted(
                {
                    key
                    for keys in deps.result_mappers.all_meaningful_change_keys().values()
                    for key in keys
                }
            )
        ),
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
    fingerprint = await canonical_progress_fingerprint(deps, runtime, step=runtime.step_count + 1)
    no_progress = runtime.no_progress_turns
    if fingerprint is not None:
        no_progress = no_progress + 1 if runtime.last_progress_fingerprint == fingerprint else 0
    else:
        fingerprint = runtime.last_progress_fingerprint
    task_id = runtime.active_task_id
    claimed_agent: Any | None = None
    if runtime.mission_id is not None:
        from redteam.orchestration.coordination import (
            CLOSED,
            StateConflict,
            coordinator_for,
        )
        from redteam.orchestration.operator_intents.service import _is_suppressed
        from redteam.orchestration.task_graph.claim import claim_mission_task

        owner = runtime.work_owner
        now = datetime.now(timezone.utc)
        coordinator = deps.coordinator or coordinator_for(
            deps.task_graph_store, deps.path_store
        )

        def _role_ok(item: Any) -> bool:
            # ``assigned_agent`` is a role, never an instance owner. An
            # instance only takes tasks of its own role (or unassigned
            # ones); it never seizes another role's task.
            assigned = getattr(item, "assigned_agent", None)
            if assigned is None or runtime.current_agent is None:
                return True
            return str(assigned) == str(runtime.current_agent)

        async def _claim_next(change: Any) -> dict[str, Any]:
            # One fresh transactional view: readiness bookkeeping and the
            # instance-targeted claim commit together or not at all. No
            # outside I/O runs in here; events publish after the commit.
            mission = change.tasks.peek_mission(runtime.mission_id)
            if mission is None or mission.status in CLOSED:
                return {
                    "task_id": None,
                    "assigned_agent": None,
                    "readied": [],
                    "unblocked": [],
                    "started": False,
                }
            staged = await change.tasks.list_tasks(mission_id=runtime.mission_id)
            # Dependency readiness is bookkeeping, not a strategy decision.
            # Only release tasks already registered by the operator/planning
            # layer.
            by_id = {item.task_id: item for item in staged}
            readied: list[Any] = []
            unblocked: list[Any] = []
            for item in staged:
                if item.status not in {TaskStatus.PENDING, TaskStatus.BLOCKED}:
                    continue
                if _is_suppressed(item, mission):
                    continue
                if not item.dependency_task_ids:
                    continue
                if not all(
                    identity in by_id
                    and by_id[identity].status is TaskStatus.COMPLETED
                    for identity in item.dependency_task_ids
                ):
                    continue
                if item.status is TaskStatus.PENDING:
                    await change.tasks.transition_task(
                        item.task_id, TaskStatus.READY, now=now
                    )
                    readied.append(item.task_id)
                elif item.blocking_reason == "required dependency is blocked":
                    await change.tasks.unblock_task(item.task_id, now=now)
                    unblocked.append(item.task_id)
            staged = await change.tasks.list_tasks(mission_id=runtime.mission_id)
            candidates = [
                item
                for item in staged
                if item.status in {TaskStatus.READY, TaskStatus.RUNNING}
                and not _is_suppressed(item, mission)
                and _role_ok(item)
            ]
            # Prefer the already-active task so an instance sticks to its
            # own work instead of hopping between tasks each turn.
            if task_id is not None:
                candidates.sort(key=lambda item: (item.task_id != task_id))
            for item in candidates:
                try:
                    claimed = await claim_mission_task(
                        change, item.task_id, owner=owner, now=now
                    )
                except StateConflict:
                    continue
                return {
                    "task_id": claimed.task_id,
                    "assigned_agent": getattr(claimed, "assigned_agent", None),
                    "readied": readied,
                    "unblocked": unblocked,
                    "started": item.status is TaskStatus.READY,
                }
            return {
                "task_id": None,
                "assigned_agent": None,
                "readied": readied,
                "unblocked": unblocked,
                "started": False,
            }

        result = await coordinator.change(_claim_next)
        task_id = result["task_id"]
        for item_id in result["unblocked"]:
            event = await publisher.publish(
                EventType.TASK_UNBLOCKED,
                {"task_id": item_id, "reason": "dependencies_completed"},
                causation_id=last_event_id,
            )
            last_event_id = event.event_id
        if task_id is not None and result["started"]:
            event = await publisher.publish(
                EventType.TASK_STARTED,
                {"mission_id": runtime.mission_id, "task_id": task_id},
                causation_id=last_event_id,
            )
            last_event_id = event.event_id
        claimed_agent = result["assigned_agent"]
    # This selects the coordinator identity, not a tool, strategy or first scan.
    agent = deps.agents.get(runtime.current_agent) if runtime.current_agent else None
    if agent is None and claimed_agent is not None:
        agent = deps.agents.get(claimed_agent)
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
