"""Dynamic replanning: turn new knowledge in the Attack Graph into Task Graph
changes, and evaluate Mission progress.

This is the piece that makes the Orchestrator more than a static checklist: as
recon/web work reveals attack surface, the Task Graph grows to match. The
canonical case is an object-bound API endpoint (``/api/orders/{id}``): its
existence implies an authorization question, so a dedicated assessment Task is
created. Task creation coincides with a graph change, so the existing
"graph changed -> continue" replan path keeps the run going and OBSERVE
schedules the new Task on the next turn (Task-first).

It never touches the Attack Graph, Work Ledger or Evidence — only the Task
Graph — and never stores a secret.
"""

from __future__ import annotations

from datetime import datetime, timezone

from redteam.attack_graph.predicates import facts_of
from redteam.attack_graph.service import AttackGraphService
from redteam.core.contracts.events import EventType
from redteam.core.contracts.mission import MissionTask, Provenance
from redteam.core.enums import GraphNodeType, MissionStatus, TaskStatus
from redteam.core.ids import EventId, MissionId, TaskId, new_id
from redteam.core.ports.task_graph import MissionTaskStore
from redteam.event_bus.publisher import EventPublisher

_TERMINAL = {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}
#: Provenance marker for the single "acquire a second principal" Task, so the
#: dependency chain is created once per Mission, not per endpoint.
_ACQUIRE_SOURCE = "replan:acquire_second_principal"


async def replan_task_graph(
    store: MissionTaskStore,
    graph: AttackGraphService,
    mission_id: MissionId,
    *,
    publisher: EventPublisher,
    last_event_id: EventId | None,
    now: datetime | None = None,
) -> EventId | None:
    """Create a Task for each newly-discovered object-bound endpoint.

    Idempotent: an endpoint already covered by a Task (by
    ``related_attack_graph_node_ids``) is skipped, so replanning every turn
    never duplicates work.
    """

    now = now or datetime.now(timezone.utc)
    nodes = await graph.list_nodes()
    principals = sum(1 for node in nodes if node.type is GraphNodeType.PRINCIPAL)

    existing = await store.list_tasks(mission_id=mission_id)
    covered = {
        str(node_id) for task in existing for node_id in task.related_attack_graph_node_ids
    }
    acquire_task_id = next(
        (task.task_id for task in existing if task.provenance.source == _ACQUIRE_SOURCE),
        None,
    )

    for node in nodes:
        if node.type is not GraphNodeType.ENDPOINT:
            continue
        params = facts_of(node.properties).get("path_parameters")
        if not params or str(node.id) in covered:
            continue
        # An object-level authorization check compares two principals' access
        # to the same object. With fewer than two principals known, the check
        # is blocked on acquiring a second one, expressed as a dependency Task.
        needs_second_principal = principals < 2
        auth_task = MissionTask(
            task_id=TaskId(new_id("task")),
            mission_id=mission_id,
            title=f"Authorization assessment: {node.id}",
            objective=(
                f"Verify object-level authorization on the object-bound endpoint {node.id}"
            ),
            status=TaskStatus.READY,
            #: High value: object-bound authz outranks the generic role sweeps.
            priority=100,
            required_capability="web",
            related_attack_graph_node_ids=[node.id],
            provenance=Provenance(source="replan:object_bound_endpoint"),
            created_at=now,
            updated_at=now,
        )
        await store.create_task(auth_task)
        last_event_id = await _publish_task_created(
            publisher, auth_task.task_id, "object_bound_endpoint", str(node.id), last_event_id
        )

        if needs_second_principal:
            if acquire_task_id is None:
                acquire = MissionTask(
                    task_id=TaskId(new_id("task")),
                    mission_id=mission_id,
                    title="Acquire a second principal",
                    objective="Obtain a second principal/session to compare authorization",
                    status=TaskStatus.READY,
                    priority=101,
                    required_capability="web",
                    provenance=Provenance(source=_ACQUIRE_SOURCE),
                    created_at=now,
                    updated_at=now,
                )
                await store.create_task(acquire)
                acquire_task_id = acquire.task_id
                last_event_id = await _publish_task_created(
                    publisher, acquire.task_id, "needs_second_principal", "", last_event_id
                )
            # Depend the authz check on the acquisition; add_dependency demotes
            # it READY -> PENDING so it is not scheduled until the dep completes.
            await store.add_dependency(auth_task.task_id, acquire_task_id, now=now)

    # Promotion: a PENDING Task whose dependencies are all COMPLETED becomes
    # schedulable again (the unblock half of the dependency chain).
    for task in await store.list_tasks(mission_id=mission_id, status=TaskStatus.PENDING):
        deps = [await store.get_task(dep) for dep in task.dependency_task_ids]
        if deps and all(dep is not None and dep.status is TaskStatus.COMPLETED for dep in deps):
            await store.transition_task(task.task_id, TaskStatus.READY, now=now)

    return last_event_id


async def _publish_task_created(
    publisher: EventPublisher,
    task_id: TaskId,
    reason: str,
    endpoint: str,
    last_event_id: EventId | None,
) -> EventId | None:
    event = await publisher.publish(
        EventType.TASK_CREATED,
        {"task_id": task_id, "reason": reason, "endpoint": endpoint},
        causation_id=last_event_id,
    )
    return event.event_id


async def finalize_mission(
    store: MissionTaskStore,
    mission_id: MissionId,
    *,
    publisher: EventPublisher,
    last_event_id: EventId | None,
    now: datetime | None = None,
) -> EventId | None:
    """Settle the Mission from its final Task states.

    COMPLETED when every Task reached a terminal state and none failed;
    otherwise BLOCKED (required work could not be finished). Mirrors success
    onto ``MissionStatus`` so a report can state the outcome.
    """

    mission = await store.get_mission(mission_id)
    if mission is None or mission.status in {MissionStatus.COMPLETED, MissionStatus.FAILED}:
        return last_event_id

    tasks = await store.list_tasks(mission_id=mission_id)
    if not tasks:
        return last_event_id
    now = now or datetime.now(timezone.utc)

    # The run has ended, so no Task can make further progress. Settle every
    # non-terminal Task to a terminal state so the Mission can be judged:
    # a RUNNING Task's agent had finished (COMPLETED); a Task that was never
    # scheduled -- no attack surface for its role, or a dependency never met
    # -- is CANCELLED (not reached), which is not a failure.
    for task in tasks:
        if task.status is TaskStatus.RUNNING:
            await store.transition_task(task.task_id, TaskStatus.COMPLETED, now=now)
        elif task.status not in _TERMINAL:
            await store.transition_task(
                task.task_id, TaskStatus.CANCELLED, now=now, reason="run ended before scheduling"
            )

    settled = await store.list_tasks(mission_id=mission_id)
    any_failed = any(task.status is TaskStatus.FAILED for task in settled)
    outcome = MissionStatus.FAILED if any_failed else MissionStatus.COMPLETED
    await store.update_mission(
        mission.model_copy(
            update={"status": outcome, "revision": mission.revision + 1, "updated_at": now}
        )
    )
    event = await publisher.publish(
        EventType.MISSION_UPDATED,
        {"mission_id": mission_id, "status": outcome.value},
        causation_id=last_event_id,
    )
    return event.event_id
