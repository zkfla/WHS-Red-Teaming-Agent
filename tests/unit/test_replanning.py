"""Phase 3 (Slice B): dynamic replanning grows the Task Graph from knowledge."""

from datetime import datetime, timezone
from typing import Any

import pytest

from redteam.attack_graph.service import AttackGraphService
from redteam.attack_graph.stores.memory import InMemoryAttackGraphStore
from redteam.core.contracts.events import EventEnvelope
from redteam.core.contracts.evidence import EvidenceRecord
from redteam.core.contracts.mission import MissionPlan, MissionTask, Provenance
from redteam.core.enums import EvidenceSourceType, GraphNodeType, MissionStatus, TaskStatus
from redteam.core.ids import EngagementId, EvidenceId, MissionId, RunId, TaskId
from redteam.event_bus.memory import InMemoryEventBus
from redteam.event_bus.publisher import EventPublisher
from redteam.orchestration.replanning import (
    _evaluate_criterion,
    finalize_mission,
    replan_task_graph,
)
from redteam.orchestration.task_graph.memory import InMemoryTaskGraphStore
from tests.fakes import FakeEvidenceStore

MISSION = MissionId("m_replan")


class _Publisher(EventPublisher):
    """Boundary-compatible recorder: a real publisher that keeps the old event log."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(
            InMemoryEventBus(),
            run_id=RunId("run_replan"),
            engagement_id=EngagementId("eng"),
            source="test.replan",
        )
        self.events: list[tuple[str, dict[str, Any]]] = []

    async def publish(
        self,
        event_type: str,
        payload: dict[str, Any] | None = None,
        *,
        correlation_id: str | None = None,
        causation_id: str | None = None,
    ) -> EventEnvelope:
        event = await super().publish(
            event_type, payload, correlation_id=correlation_id, causation_id=causation_id
        )
        self.events.append((event_type, dict(payload or {})))
        return event


async def _required_task(store: InMemoryTaskGraphStore, task_id: TaskId) -> MissionTask:
    found = await store.get_task(task_id)
    assert found is not None
    return found


async def _required_mission(store: InMemoryTaskGraphStore) -> MissionPlan:
    found = await store.get_mission(MISSION)
    assert found is not None
    return found


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


async def _graph_with_endpoints(
    *, principals: int = 0, sessions: bool = True
) -> AttackGraphService:
    service = AttackGraphService(InMemoryAttackGraphStore())
    await service.upsert_node(
        GraphNodeType.HOST,
        key="mock-bank",
        facts={
            "target_ref": "bank",
            "auth_session_config": {
                "sessions": [
                    {"session_ref": "alice", "paths": ["/api/orders/1234"]},
                    {"session_ref": "bob", "paths": ["/api/orders/1234"]},
                ]
                if sessions
                else []
            },
        },
    )
    # object-bound: path has an id parameter -> authorization question
    await service.upsert_node(
        GraphNodeType.ENDPOINT,
        key="mock-bank:8080 GET /api/orders/{id}",
        facts={
            "path_parameters": ["orders_id"],
            "host": "mock-bank",
            "path": "/api/orders/1234",
            "target_ref": "bank",
        },
    )
    # collection endpoint: no id parameter -> no authorization task
    await service.upsert_node(
        GraphNodeType.ENDPOINT,
        key="mock-bank:8080 GET /api/orders",
        facts={"path_parameters": []},
    )
    for index in range(principals):
        await service.upsert_node(
            GraphNodeType.PRINCIPAL,
            key=f"principal-{index}",
            facts={"target_ref": "bank", "principal_ref": f"principal-{index}"},
        )
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


async def test_no_configured_sessions_blocks_acquisition_with_reason() -> None:
    store = await _store_with_mission()
    graph = await _graph_with_endpoints(principals=0, sessions=False)
    await replan_task_graph(store, graph, MISSION, publisher=_Publisher(), last_event_id=None)
    tasks = await store.list_tasks(mission_id=MISSION)
    assert _acquire_task(tasks).status is TaskStatus.BLOCKED
    assert "session references" in _acquire_task(tasks).blocking_reason
    assert _auth_task(tasks).status is TaskStatus.BLOCKED


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


async def test_blocked_dependency_blocks_authz_then_observation_recovers_it() -> None:
    store = await _store_with_mission()
    graph = await _graph_with_endpoints(principals=1)
    publisher = _Publisher()
    await replan_task_graph(store, graph, MISSION, publisher=publisher, last_event_id=None)
    acquire = _acquire_task(await store.list_tasks(mission_id=MISSION))
    await store.block_task(acquire.task_id, "no session available", now=_now())

    await replan_task_graph(store, graph, MISSION, publisher=publisher, last_event_id=None)
    auth = _auth_task(await store.list_tasks(mission_id=MISSION))
    assert auth.status is TaskStatus.BLOCKED
    assert auth.blocking_reason == "required dependency is blocked"

    await graph.upsert_node(
        GraphNodeType.PRINCIPAL,
        key="principal-2",
        facts={"target_ref": "bank", "principal_ref": "principal-2"},
    )
    await replan_task_graph(store, graph, MISSION, publisher=publisher, last_event_id=None)
    assert (await _required_task(store, acquire.task_id)).status is TaskStatus.COMPLETED
    assert (await _required_task(store, auth.task_id)).status is TaskStatus.READY
    assert "TaskUnblocked" in [event[0] for event in publisher.events]


async def test_replanning_is_idempotent() -> None:
    store = await _store_with_mission()
    graph = await _graph_with_endpoints(principals=2)
    publisher = _Publisher()

    await replan_task_graph(store, graph, MISSION, publisher=publisher, last_event_id=None)
    await replan_task_graph(store, graph, MISSION, publisher=publisher, last_event_id=None)

    # The endpoint is already covered on the second pass -> no duplicate Task.
    assert len(await store.list_tasks(mission_id=MISSION)) == 1


async def _replan_after_comparison(
    first_status: int, second_status: int, evidence_case: str = "valid"
) -> Any:
    """Two principals -> authz task READY, then a session comparison lands on
    the endpoint with the given statuses. Returns the authz task afterwards."""
    store = await _store_with_mission()
    graph = await _graph_with_endpoints(principals=2)
    await replan_task_graph(store, graph, MISSION, publisher=_Publisher(), last_event_id=None)
    auth = _auth_task(await store.list_tasks(mission_id=MISSION))
    observation = {
        "kind": "http_session_comparison",
        "first_status": first_status,
        "second_status": second_status,
        "evidence_id": "ev_compare",
    }
    await graph.upsert_node(
        GraphNodeType.ENDPOINT,
        key="mock-bank:8080 GET /api/orders/{id}",
        facts={"observations": [observation]},
    )
    records = FakeEvidenceStore()
    if evidence_case != "missing":
        await records.append(
            EvidenceRecord(
                evidence_id=EvidenceId("ev_compare"),
                run_id=RunId("old_run" if evidence_case == "stale" else "run_comparison"),
                engagement_id=EngagementId("other" if evidence_case == "foreign" else "eng"),
                source_type=EvidenceSourceType.TOOL,
                source_id="web.http_compare_sessions",
                content_type="application/json",
                digest="comparison",
                created_at=_now(),
                metadata={
                    "success": evidence_case != "failed",
                    "mission_task_id": "other_task"
                    if evidence_case == "wrong_task"
                    else auth.task_id,
                    "target_node_id": str(auth.related_attack_graph_node_ids[0]),
                    "session_comparison": observation,
                },
            )
        )
    await replan_task_graph(
        store,
        graph,
        MISSION,
        publisher=_Publisher(),
        last_event_id=None,
        evidence_store=records,
        run_id=RunId("run_comparison"),
    )
    return _auth_task(await store.list_tasks(mission_id=MISSION))


async def test_conclusive_comparison_completes_the_authz_task() -> None:
    auth = await _replan_after_comparison(200, 403)
    assert auth.status is TaskStatus.COMPLETED


async def test_two_server_errors_do_not_complete_the_authz_task() -> None:
    # Both sessions 500 -> the comparison proves nothing about authorization.
    # The task must stay open, not be marked done on an inconclusive result.
    auth = await _replan_after_comparison(500, 500)
    assert auth.status is not TaskStatus.COMPLETED


@pytest.mark.parametrize(
    "statuses", [(401, 401), (200, 401), (403, 403), (200, 404), (200, 302), (200, 500)]
)
async def test_inconclusive_comparison_keeps_authorization_unfinished(statuses) -> None:
    assert (await _replan_after_comparison(*statuses)).status is not TaskStatus.COMPLETED


@pytest.mark.parametrize("case", ["missing", "stale", "foreign", "wrong_task", "failed"])
async def test_graph_observation_needs_current_matching_evidence(case) -> None:
    assert (await _replan_after_comparison(200, 403, case)).status is not TaskStatus.COMPLETED


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


async def test_finalize_preserves_unfinished_required_tasks_and_blocks_mission() -> None:
    # A stopped run does not prove that an active or unstarted required Task
    # met its objective. Their state remains visible for diagnosis/resume.
    store = await _store_with_mission()
    await store.create_task(_task("ran", TaskStatus.READY))
    await store.create_task(_task("never", TaskStatus.READY))
    await store.transition_task(TaskId("ran"), TaskStatus.RUNNING, now=_now())

    publisher = _Publisher()
    await finalize_mission(store, MISSION, publisher=publisher, last_event_id=None)

    tasks = {t.task_id: t.status for t in await store.list_tasks(mission_id=MISSION)}
    assert tasks[TaskId("ran")] is TaskStatus.RUNNING
    assert tasks[TaskId("never")] is TaskStatus.READY
    assert (await _required_mission(store)).status is MissionStatus.BLOCKED
    assert [event[0] for event in publisher.events] == ["MissionUpdated"]


async def test_finalize_marks_mission_failed_when_a_task_failed() -> None:
    store = await _store_with_mission()
    await store.create_task(_task("boom", TaskStatus.READY))
    await store.transition_task(TaskId("boom"), TaskStatus.RUNNING, now=_now())
    await store.transition_task(TaskId("boom"), TaskStatus.FAILED, now=_now())

    await finalize_mission(store, MISSION, publisher=_Publisher(), last_event_id=None)
    assert (await _required_mission(store)).status is MissionStatus.FAILED


async def test_finalize_completes_only_when_required_tasks_complete() -> None:
    store = await _store_with_mission()
    required = _task("required", TaskStatus.READY)
    optional = _task("optional", TaskStatus.READY).model_copy(update={"required": False})
    await store.create_task(required)
    await store.create_task(optional)
    await store.transition_task(required.task_id, TaskStatus.RUNNING, now=_now())
    await store.transition_task(required.task_id, TaskStatus.COMPLETED, now=_now())

    await finalize_mission(store, MISSION, publisher=_Publisher(), last_event_id=None)

    assert (await _required_mission(store)).status is MissionStatus.COMPLETED
    assert (await _required_task(store, optional.task_id)).status is TaskStatus.CANCELLED


async def test_criterion_requires_evidence_from_its_completed_task() -> None:
    store = await _store_with_mission()
    mission = await _required_mission(store)
    await store.update_mission(
        mission.model_copy(
            update={
                "success_criteria": ["verified evidence for reconnaissance"],
                "revision": mission.revision + 1,
            }
        )
    )
    task = _task("recon_task", TaskStatus.READY).model_copy(
        update={
            "required_capability": "recon",
            "provenance": Provenance(source="engagement:bootstrap"),
        }
    )
    await store.create_task(task)
    await store.transition_task(task.task_id, TaskStatus.RUNNING, now=_now())
    await store.transition_task(task.task_id, TaskStatus.COMPLETED, now=_now())
    records = FakeEvidenceStore()
    run_id = RunId("run_criteria")
    await records.append(
        EvidenceRecord(
            evidence_id=EvidenceId("ev_wrong"),
            run_id=run_id,
            engagement_id=EngagementId("eng"),
            source_type=EvidenceSourceType.TOOL,
            source_id="recon.http_probe",
            content_type="application/json",
            digest="x",
            created_at=_now(),
            metadata={"success": True, "mission_task_id": "another_task"},
        )
    )
    await finalize_mission(
        store,
        MISSION,
        publisher=_Publisher(),
        last_event_id=None,
        evidence_store=records,
        run_id=run_id,
    )
    blocked = await _required_mission(store)
    assert blocked.status is MissionStatus.BLOCKED
    assert blocked.criterion_results[0].met is False
    assert blocked.criterion_results[0].reason == "matching Task has no successful Evidence"

    # A second active Mission with evidence tied to its own Task can complete.
    store = await _store_with_mission()
    mission = await _required_mission(store)
    await store.update_mission(
        mission.model_copy(
            update={
                "success_criteria": ["verified evidence for reconnaissance"],
                "revision": mission.revision + 1,
            }
        )
    )
    await store.create_task(task)
    await store.transition_task(task.task_id, TaskStatus.RUNNING, now=_now())
    await store.transition_task(task.task_id, TaskStatus.COMPLETED, now=_now())
    await records.append(
        EvidenceRecord(
            evidence_id=EvidenceId("ev_right"),
            run_id=run_id,
            engagement_id=EngagementId("eng"),
            source_type=EvidenceSourceType.TOOL,
            source_id="recon.http_probe",
            content_type="application/json",
            digest="y",
            created_at=_now(),
            metadata={"success": True, "mission_task_id": str(task.task_id)},
        )
    )
    await finalize_mission(
        store,
        MISSION,
        publisher=_Publisher(),
        last_event_id=None,
        evidence_store=records,
        run_id=run_id,
    )
    completed = await _required_mission(store)
    assert completed.status is MissionStatus.COMPLETED
    assert completed.criterion_results[0].evidence_ids == ["ev_right"]


async def test_no_tasks_cannot_complete_a_mission() -> None:
    store = await _store_with_mission()
    await finalize_mission(store, MISSION, publisher=_Publisher(), last_event_id=None)
    assert (await _required_mission(store)).status is MissionStatus.BLOCKED


def test_each_required_task_needs_its_own_evidence() -> None:
    tasks = [
        _task(name, TaskStatus.COMPLETED).model_copy(
            update={
                "required_capability": "recon",
                "provenance": Provenance(source="engagement:bootstrap"),
            }
        )
        for name in ("one", "two")
    ]
    records = [
        EvidenceRecord(
            evidence_id=EvidenceId(f"ev_{task.task_id}"),
            run_id=RunId("run_criteria"),
            engagement_id=EngagementId("eng"),
            source_type=EvidenceSourceType.TOOL,
            source_id="recon.http_probe",
            content_type="application/json",
            digest="probe",
            created_at=_now(),
            metadata={"success": True, "mission_task_id": str(task.task_id)},
        )
        for task in tasks
    ]
    criterion = "verified evidence for reconnaissance"
    partial = _evaluate_criterion(criterion, tasks, records[:1])
    assert not partial.met
    assert set(partial.task_ids) == {t.task_id for t in tasks}
    assert _evaluate_criterion(criterion, tasks, records).met
    assert not _evaluate_criterion("unimplemented operator criterion", tasks, records).met


@pytest.mark.parametrize(
    "change",
    [
        {"source_id": "web.http_request"},
        {"metadata": {"success": False, "mission_task_id": "one"}},
    ],
)
def test_success_in_another_capability_or_failed_tool_is_not_evidence(change) -> None:
    task = _task("one", TaskStatus.COMPLETED).model_copy(
        update={
            "required_capability": "recon",
            "provenance": Provenance(source="engagement:bootstrap"),
        }
    )
    record = EvidenceRecord(
        evidence_id=EvidenceId("ev"),
        run_id=RunId("run"),
        engagement_id=EngagementId("eng"),
        source_type=EvidenceSourceType.TOOL,
        source_id="recon.http_probe",
        content_type="application/json",
        digest="probe",
        created_at=_now(),
        metadata={"success": True, "mission_task_id": "one"},
    ).model_copy(update=change)
    assert not _evaluate_criterion("verified evidence for reconnaissance", [task], [record]).met
