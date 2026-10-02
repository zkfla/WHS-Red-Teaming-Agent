"""Atomic effect, run ownership, and leased delivery boundaries."""

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from redteam.core.contracts.events import EventType
from redteam.core.contracts.mission import MissionPlan, MissionTask, Provenance
from redteam.core.enums import MissionStatus, TaskStatus
from redteam.event_bus.memory import InMemoryEventBus
from redteam.event_bus.publisher import EventPublisher
from redteam.orchestration.coordination import (
    CoordinationError,
    StateConflict,
    coordinator_for,
)
from redteam.orchestration.operator_intents import (
    InMemoryOperatorIntentStore,
    IntentStatus,
    OperatorIntentService,
    SQLiteOperatorIntentStore,
)
from redteam.orchestration.outbox import deliver_pending
from redteam.orchestration.paths import InMemoryPathStore, SQLitePathStore
from redteam.orchestration.task_graph.memory import InMemoryTaskGraphStore
from redteam.orchestration.task_graph.sqlite import SQLiteTaskGraphStore

NOW = datetime(2026, 10, 1, tzinfo=timezone.utc)
RUN, MISSION, ENG = "run-atomic", "mission-atomic", "eng-atomic"


async def world(tmp_path, backend, *, bus=None):
    db = tmp_path / "coordination.sqlite"
    if backend == "sqlite":
        tasks = SQLiteTaskGraphStore(db)
        paths = SQLitePathStore(db)
        history = SQLiteOperatorIntentStore(tmp_path / "history.sqlite")
    else:
        tasks, paths, history = (
            InMemoryTaskGraphStore(),
            InMemoryPathStore(),
            InMemoryOperatorIntentStore(),
        )
    await tasks.create_mission(
        MissionPlan(
            mission_id=MISSION,
            engagement_id=ENG,
            objective="Assess the authorized target",
            status=MissionStatus.ACTIVE,
            created_at=NOW,
            updated_at=NOW,
            provenance=Provenance(source="test"),
        )
    )
    for identity in ["task-a", "task-b"]:
        await tasks.create_task(
            MissionTask(
                task_id=identity,
                mission_id=MISSION,
                title=identity,
                objective="web investigation",
                status=TaskStatus.READY,
                priority=10,
                required_capability="web",
                created_at=NOW,
                updated_at=NOW,
                provenance=Provenance(source="test"),
            )
        )
    bus = bus if bus is not None else InMemoryEventBus()
    coordinator = coordinator_for(tasks, paths)
    await coordinator.bind(RUN, MISSION, ENG)
    service = OperatorIntentService(
        store=history,
        task_graph_store=tasks,
        path_store=paths,
        event_bus=bus,
    )
    publisher = EventPublisher(bus, run_id=RUN, engagement_id=ENG, source="test")
    return SimpleNamespace(
        tasks=tasks,
        paths=paths,
        history=history,
        bus=bus,
        coordinator=coordinator,
        service=service,
        publisher=publisher,
        db=db,
    )


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
async def test_stale_received_copy_and_two_services_apply_one_effect(tmp_path, backend):
    w = await world(tmp_path, backend)
    received = await w.service.receive(
        "focus web",
        mission_id=MISSION,
        run_id=RUN,
        publisher=w.publisher,
    )
    stale = (await w.history.get(received.intent_id)).model_copy(deep=True)
    second = OperatorIntentService(
        store=w.history,
        task_graph_store=w.tasks,
        path_store=w.paths,
        event_bus=w.bus,
    )
    before = await w.tasks.get_mission(MISSION)
    results = await asyncio.gather(
        *(
            service.apply(stale, run_id=RUN, publisher=w.publisher)
            for service in [w.service, second]
        )
    )
    assert all(result.applied for result in results)
    after = await w.tasks.get_mission(MISSION)
    assert after.revision == before.revision + 1
    assert (await w.history.get(received.intent_id)).status is IntentStatus.APPLIED
    assert len(w.bus.history(event_type=EventType.OPERATOR_INTENT_APPLIED)) == 1
    count = len(w.bus.history())
    await second.apply(stale, run_id=RUN, publisher=w.publisher)
    assert len(w.bus.history()) == count
    assert (await w.tasks.get_mission(MISSION)).revision == after.revision


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
async def test_registration_blocks_review_and_invalidates_old_token(tmp_path, backend):
    w = await world(tmp_path, backend)
    token = await w.coordinator.review_token(RUN, MISSION)
    received = await w.service.receive("focus web", mission_id=MISSION, run_id=RUN)
    with pytest.raises(StateConflict, match="pending_operator_mutation"):
        await w.coordinator.review_token(RUN, MISSION)
    await w.service.apply(received.intent_id, run_id=RUN)

    async def validate(change):
        change.admit(RUN, MISSION, token=token)

    with pytest.raises(StateConflict, match="reviewed_state_changed"):
        await w.coordinator.change(validate)


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
@pytest.mark.parametrize("instruction", ["focus web", "stop"])
async def test_received_instruction_cannot_apply_after_run_closes(tmp_path, backend, instruction):
    w = await world(tmp_path, backend)
    received = await w.service.receive(
        instruction, mission_id=MISSION, run_id=RUN, publisher=w.publisher
    )
    mission_before = await w.tasks.get_mission(MISSION)
    tasks_before = await w.tasks.list_tasks(mission_id=MISSION)

    async def close(change):
        change.binding(RUN, MISSION, ENG)["closed"] = True

    await w.coordinator.change(close)
    result = await w.service.apply(received.intent_id, run_id=RUN, publisher=w.publisher)
    assert not result.applied
    assert result.reason == "mission_closed"
    assert await w.tasks.get_mission(MISSION) == mission_before
    assert await w.tasks.list_tasks(mission_id=MISSION) == tasks_before
    assert (await w.history.get(received.intent_id)).status is IntentStatus.REJECTED
    assert not w.bus.history(event_type=EventType.OPERATOR_INTENT_APPLIED)
    event_count = len(w.bus.history())
    replay = await w.service.apply(received.intent_id, run_id=RUN, publisher=w.publisher)
    assert replay == result
    assert len(w.bus.history()) == event_count


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
@pytest.mark.parametrize(
    "status", [MissionStatus.COMPLETED, MissionStatus.FAILED, MissionStatus.CANCELLED]
)
async def test_initial_binding_of_closed_mission_is_closed(tmp_path, backend, status):
    w = await world(tmp_path, backend)
    mission = await w.tasks.get_mission(MISSION)
    closed_mission = "mission-already-closed"
    closed_run = "run-already-closed"
    await w.tasks.create_mission(
        mission.model_copy(update={"mission_id": closed_mission, "status": status})
    )
    await w.coordinator.bind(closed_run, closed_mission, ENG)

    async def read(change):
        return change.binding(closed_run, closed_mission, ENG)["closed"]

    assert await w.coordinator.change(read)


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
async def test_failure_rolls_back_all_task_mission_and_internal_changes(tmp_path, backend):
    w = await world(tmp_path, backend)
    before = await w.tasks.get_mission(MISSION)

    async def fail(change):
        await change.tasks.update_priority("task-a", 900, now=NOW)
        await change.tasks.update_priority("task-b", 800, now=NOW)
        mission = await change.tasks.get_mission(MISSION)
        await change.tasks.update_mission(
            mission.model_copy(
                update={
                    "revision": mission.revision + 1,
                    "operator_preferences": {"focus_areas": ["web"]},
                }
            )
        )
        change.records[("test", "rollback")] = {"effect": True}
        raise ValueError("validation_failed")

    with pytest.raises(ValueError, match="validation_failed"):
        await w.coordinator.change(fail)
    assert await w.tasks.get_mission(MISSION) == before
    assert all(task.priority == 10 for task in await w.tasks.list_tasks(mission_id=MISSION))

    async def inspect(change):
        return ("test", "rollback") in change.records

    assert not await w.coordinator.change(inspect)


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
async def test_foreign_run_and_publisher_cannot_register_or_apply(tmp_path, backend):
    w = await world(tmp_path, backend)
    before = await w.tasks.get_mission(MISSION)
    rejected = await w.service.receive("focus web", mission_id=MISSION, run_id="foreign-run")
    assert rejected.status is IntentStatus.REJECTED
    assert await w.history.list_for_mission(MISSION) == []
    received = await w.service.receive("focus web", mission_id=MISSION, run_id=RUN)
    wrong = EventPublisher(w.bus, run_id="foreign-run", engagement_id=ENG, source="test")
    application = await w.service.apply(received.intent_id, run_id=RUN, publisher=wrong)
    assert not application.applied
    assert "publisher_binding_mismatched" in application.reason
    assert await w.tasks.get_mission(MISSION) == before
    assert (await w.history.get(received.intent_id)).status is IntentStatus.RECEIVED
    assert not w.bus.history()


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
async def test_late_stop_preserves_completed_mission_and_tasks(tmp_path, backend):
    w = await world(tmp_path, backend)
    mission = await w.tasks.get_mission(MISSION)
    await w.tasks.update_mission(
        mission.model_copy(
            update={
                "status": MissionStatus.COMPLETED,
                "revision": mission.revision + 1,
            }
        )
    )
    before = await w.tasks.get_mission(MISSION)
    task_before = await w.tasks.list_tasks(mission_id=MISSION)
    result = await w.service.receive_and_apply(
        "stop",
        mission_id=MISSION,
        run_id=RUN,
        publisher=w.publisher,
    )
    assert not result.applied and result.outcome != "mission_stopped"
    assert result.status is IntentStatus.REJECTED
    assert result.reason == "mission_closed"
    assert await w.tasks.get_mission(MISSION) == before
    assert await w.tasks.list_tasks(mission_id=MISSION) == task_before
    assert not w.bus.history(event_type=EventType.OPERATOR_INTENT_APPLIED)


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
async def test_delivery_failure_replays_stable_envelope_after_expired_lease(
    tmp_path, backend, monkeypatch
):
    clock = [1000.0]
    monkeypatch.setattr("redteam.orchestration.outbox.time.time", lambda: clock[0])

    class FailingBus:
        async def publish(self, event):
            raise RuntimeError("transport unavailable")

    w = await world(tmp_path, backend, bus=FailingBus())
    received = await w.service.receive(
        "focus web",
        mission_id=MISSION,
        run_id=RUN,
        publisher=w.publisher,
    )
    await w.service.apply(received.intent_id, run_id=RUN, publisher=w.publisher)
    mission = await w.tasks.get_mission(MISSION)
    bus = InMemoryEventBus()
    remaining = await deliver_pending(
        w.coordinator,
        run_id=RUN,
        engagement_id=ENG,
        event_bus=bus,
    )
    assert remaining > 0 and not bus.history()
    clock[0] += 11
    assert (
        await deliver_pending(
            w.coordinator,
            run_id=RUN,
            engagement_id=ENG,
            event_bus=bus,
        )
        == 0
    )
    received_record = await w.history.get(received.intent_id)
    ids = {str(event.event_id) for event in bus.history()}
    assert str(received_record.received_event_id) in ids
    assert str(received_record.applied_event_id) in ids
    assert await w.tasks.get_mission(MISSION) == mission
    count = len(bus.history())
    assert (
        await deliver_pending(
            w.coordinator,
            run_id=RUN,
            engagement_id=ENG,
            event_bus=bus,
        )
        == 0
    )
    assert len(bus.history()) == count


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
async def test_distinct_path_store_pairing_is_rejected(tmp_path, backend):
    w = await world(tmp_path, backend)
    other = InMemoryPathStore() if backend == "memory" else SQLitePathStore(w.db)
    with pytest.raises(CoordinationError, match="another_path_store"):
        coordinator_for(w.tasks, other)


async def test_independent_sqlite_instances_share_canonical_effect(tmp_path):
    w = await world(tmp_path, "sqlite")
    received = await w.service.receive("focus web", mission_id=MISSION, run_id=RUN)
    other_tasks, other_paths = SQLiteTaskGraphStore(w.db), SQLitePathStore(w.db)
    other_history = SQLiteOperatorIntentStore(tmp_path / "history.sqlite")
    other = OperatorIntentService(
        store=other_history,
        task_graph_store=other_tasks,
        path_store=other_paths,
    )
    before = await w.tasks.get_mission(MISSION)
    results = await asyncio.gather(
        w.service.apply(received.intent_id, run_id=RUN),
        other.apply(received.intent_id, run_id=RUN),
    )
    assert all(result.applied for result in results)
    assert (await w.tasks.get_mission(MISSION)).revision == before.revision + 1
    assert (await other_tasks.get_mission(MISSION)).revision == before.revision + 1
    assert (await other_history.get(received.intent_id)).status is IntentStatus.APPLIED


def _apply_in_process(db, history, intent_id):
    async def apply():
        tasks, paths = SQLiteTaskGraphStore(db), SQLitePathStore(db)
        service = OperatorIntentService(
            store=SQLiteOperatorIntentStore(history),
            task_graph_store=tasks,
            path_store=paths,
        )
        application = await service.apply(intent_id, run_id=RUN)
        return application.applied

    return asyncio.run(apply())


async def test_two_processes_apply_one_sqlite_effect(tmp_path):
    import multiprocessing
    from concurrent.futures import ProcessPoolExecutor

    w = await world(tmp_path, "sqlite")
    received = await w.service.receive("focus web", mission_id=MISSION, run_id=RUN)
    before = await w.tasks.get_mission(MISSION)
    args = (str(w.db), str(tmp_path / "history.sqlite"), str(received.intent_id))
    loop = asyncio.get_running_loop()
    with ProcessPoolExecutor(
        max_workers=2, mp_context=multiprocessing.get_context("spawn")
    ) as pool:
        results = await asyncio.wait_for(
            asyncio.gather(
                *(loop.run_in_executor(pool, _apply_in_process, *args) for _ in range(2))
            ),
            timeout=15,
        )
    assert results == [True, True]
    assert (await w.tasks.get_mission(MISSION)).revision == before.revision + 1
    assert (await w.history.get(received.intent_id)).status is IntentStatus.APPLIED


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
async def test_closed_run_allows_delivery_only_recovery_and_no_cross_run(tmp_path, backend):
    w = await world(tmp_path, backend)
    event = w.publisher.build("RecoveryTest", {"status": "committed"})
    before = await w.tasks.get_mission(MISSION)

    async def close_and_queue(change):
        change.binding(RUN, MISSION)["closed"] = True
        change.queue(event)

    await w.coordinator.change(close_and_queue)
    with pytest.raises(CoordinationError, match="mission_not_active"):
        await w.coordinator.review_token(RUN, MISSION)
    with pytest.raises(CoordinationError, match="outbox_binding_mismatched"):
        await deliver_pending(
            w.coordinator,
            run_id="foreign-run",
            engagement_id=ENG,
            event_bus=w.bus,
        )
    assert not w.bus.history()
    assert (
        await deliver_pending(
            w.coordinator,
            run_id=RUN,
            engagement_id=ENG,
            event_bus=w.bus,
        )
        == 0
    )
    assert [item.event_id for item in w.bus.history()] == [event.event_id]
    assert await w.tasks.get_mission(MISSION) == before


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
async def test_registered_change_rejects_when_mission_closes_before_apply(tmp_path, backend):
    w = await world(tmp_path, backend)
    received = await w.service.receive(
        "focus web",
        mission_id=MISSION,
        run_id=RUN,
        publisher=w.publisher,
    )
    mission = await w.tasks.get_mission(MISSION)
    closed = mission.model_copy(
        update={
            "revision": mission.revision + 1,
            "status": MissionStatus.FAILED,
        }
    )
    await w.tasks.update_mission(closed)
    task_before = await w.tasks.list_tasks(mission_id=MISSION)
    result = await w.service.apply(received.intent_id, run_id=RUN, publisher=w.publisher)
    assert result.status is IntentStatus.REJECTED
    assert result.reason == "mission_closed"
    assert await w.tasks.get_mission(MISSION) == closed
    assert await w.tasks.list_tasks(mission_id=MISSION) == task_before
    assert not w.bus.history(event_type=EventType.OPERATOR_INTENT_APPLIED)


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
async def test_duplicate_registration_has_no_generation_or_event_and_conflicting_content_rejected(
    tmp_path, backend
):
    w = await world(tmp_path, backend)
    received = await w.service.receive(
        "focus web",
        mission_id=MISSION,
        run_id=RUN,
        publisher=w.publisher,
    )
    stale = (await w.history.get(received.intent_id)).model_copy(deep=True)

    async def generation(change):
        return change.generation(RUN, MISSION)

    original = await w.coordinator.change(generation)
    event_count = len(w.bus.history())
    await w.service._register(stale, w.publisher, None)
    assert await w.coordinator.change(generation) == original
    assert len(w.bus.history()) == event_count
    stale.intent = stale.intent.model_copy(update={"instruction": "focus another authorized area"})
    with pytest.raises(CoordinationError, match="intent_content_conflict"):
        await w.service._register(stale, w.publisher, None)
    assert await w.coordinator.change(generation) == original
