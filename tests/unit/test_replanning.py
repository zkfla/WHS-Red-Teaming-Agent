"""Phase 3 (Slice B): dynamic replanning grows the Task Graph from knowledge."""

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

from redteam.attack_graph.service import AttackGraphService
from redteam.attack_graph.stores.memory import InMemoryAttackGraphStore
from redteam.core.contracts.mission import MissionPlan, MissionTask, Provenance
from redteam.core.enums import GraphNodeType, MissionStatus, TaskStatus
from redteam.core.ids import EngagementId, EventId, MissionId, TaskId
from redteam.orchestration.replanning import finalize_mission, replan_task_graph
from redteam.orchestration.task_graph.memory import InMemoryTaskGraphStore

MISSION = MissionId("m_replan")


class _Publisher:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    async def publish(self, event_type: str, payload: dict[str, Any], *, causation_id: Any = None):
        self.events.append((event_type, payload))
        return SimpleNamespace(event_id=EventId(f"evt_{len(self.events)}"))


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _store_with_mission() -> InMemoryTaskGraphStore:
    store = InMemoryTaskGraphStore()
    await store.create_mission(
        MissionPlan(
            mission_id=MISSION,
            engagement_id=EngagementId("eng"),
            objective="Assess authorization",
            status=MissionStatus.ACTIVE,
            provenance=Provenance(source="test"),
            created_at=_now(),
            updated_at=_now(),
        )
    )
    return store


async def _graph_with_endpoints(*, principals: int = 0) -> AttackGraphService:
    service = AttackGraphService(InMemoryAttackGraphStore())
    # object-bound: path has an id parameter -> authorization question
    await service.upsert_node(
        GraphNodeType.ENDPOINT,
        key="mock-bank:8080 GET /api/orders/{id}",
        facts={"path_parameters": ["orders_id"]},
    )
    # collection endpoint: no id parameter -> no authorization task
    await service.upsert_node(
        GraphNodeType.ENDPOINT,
        key="mock-bank:8080 GET /api/orders",
        facts={"path_parameters": []},
    )
    for index in range(principals):
        await service.upsert_node(GraphNodeType.PRINCIPAL, key=f"principal-{index}")
    return service


def _auth_task(tasks: list) -> Any:
    return next(t for t in tasks if t.provenance.source == "replan:object_bound_endpoint")


def _acquire_task(tasks: list) -> Any:
    return next(t for t in tasks if t.provenance.source == "replan:acquire_second_principal")


async def test_two_principals_make_the_authz_task_ready_immediately() -> None:
    store = await _store_with_mission()
    graph = await _graph_with_endpoints(principals=2)
    await replan_task_graph(store, graph, MISSION, publisher=_Publisher(), last_event_id=None)

    tasks = await store.list_tasks(mission_id=MISSION)
    assert len(tasks) == 1  # no acquisition task needed
    auth = _auth_task(tasks)
    assert auth.status is TaskStatus.READY
    assert "orders" in str(auth.related_attack_graph_node_ids[0])


async def test_missing_second_principal_blocks_authz_on_an_acquire_task() -> None:
    store = await _store_with_mission()
    graph = await _graph_with_endpoints(principals=1)
    await replan_task_graph(store, graph, MISSION, publisher=_Publisher(), last_event_id=None)

    tasks = await store.list_tasks(mission_id=MISSION)
    auth, acquire = _auth_task(tasks), _acquire_task(tasks)
    # The authz check depends on acquiring a second principal, so it is PENDING.
    assert auth.status is TaskStatus.PENDING
    assert acquire.task_id in auth.dependency_task_ids
    assert acquire.status is TaskStatus.READY


async def test_completing_acquisition_unblocks_the_authz_task() -> None:
    store = await _store_with_mission()
    graph = await _graph_with_endpoints(principals=1)
    await replan_task_graph(store, graph, MISSION, publisher=_Publisher(), last_event_id=None)
    acquire = _acquire_task(await store.list_tasks(mission_id=MISSION))

    # Acquisition completes -> a replan turn promotes the dependent authz task.
    await store.transition_task(acquire.task_id, TaskStatus.RUNNING, now=_now())
    await store.transition_task(acquire.task_id, TaskStatus.COMPLETED, now=_now())
    await replan_task_graph(store, graph, MISSION, publisher=_Publisher(), last_event_id=None)

    assert _auth_task(await store.list_tasks(mission_id=MISSION)).status is TaskStatus.READY


async def test_replanning_is_idempotent() -> None:
    store = await _store_with_mission()
    graph = await _graph_with_endpoints(principals=2)
    publisher = _Publisher()

    await replan_task_graph(store, graph, MISSION, publisher=publisher, last_event_id=None)
    await replan_task_graph(store, graph, MISSION, publisher=publisher, last_event_id=None)

    # The endpoint is already covered on the second pass -> no duplicate Task.
    assert len(await store.list_tasks(mission_id=MISSION)) == 1


def _task(task_id: str, status: TaskStatus) -> MissionTask:
    return MissionTask(
        task_id=TaskId(task_id),
        mission_id=MISSION,
        title=task_id,
        objective="do it",
        status=status,
        provenance=Provenance(source="test"),
        created_at=_now(),
        updated_at=_now(),
    )


async def test_finalize_settles_running_and_unreached_tasks() -> None:
    # At run end: a RUNNING task's agent had finished -> COMPLETED; a task that
    # was never scheduled -> CANCELLED (not a failure). Mission -> COMPLETED.
    store = await _store_with_mission()
    await store.create_task(_task("ran", TaskStatus.READY))
    await store.create_task(_task("never", TaskStatus.READY))
    await store.transition_task(TaskId("ran"), TaskStatus.RUNNING, now=_now())

    publisher = _Publisher()
    await finalize_mission(store, MISSION, publisher=publisher, last_event_id=None)

    tasks = {t.task_id: t.status for t in await store.list_tasks(mission_id=MISSION)}
    assert tasks[TaskId("ran")] is TaskStatus.COMPLETED
    assert tasks[TaskId("never")] is TaskStatus.CANCELLED
    assert (await store.get_mission(MISSION)).status is MissionStatus.COMPLETED
    assert [event[0] for event in publisher.events] == ["MissionUpdated"]


async def test_finalize_marks_mission_failed_when_a_task_failed() -> None:
    store = await _store_with_mission()
    await store.create_task(_task("boom", TaskStatus.READY))
    await store.transition_task(TaskId("boom"), TaskStatus.RUNNING, now=_now())
    await store.transition_task(TaskId("boom"), TaskStatus.FAILED, now=_now())

    await finalize_mission(store, MISSION, publisher=_Publisher(), last_event_id=None)
    assert (await store.get_mission(MISSION)).status is MissionStatus.FAILED
