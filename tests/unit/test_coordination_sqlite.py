"""SQLite orchestration commits effects, canonical outcomes and envelopes together."""

import asyncio
import copy
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timezone

import pytest

from redteam.core.contracts.events import EventType
from redteam.core.contracts.mission import MissionPlan, MissionTask, Provenance
from redteam.core.enums import MissionStatus, TaskStatus
from redteam.core.ids import EngagementId, MissionId, RunId, TaskId
from redteam.event_bus.memory import InMemoryEventBus
from redteam.event_bus.publisher import EventPublisher
from redteam.orchestration.operator_intents.interpreter import interpret_instruction
from redteam.orchestration.operator_intents.models import IntentRecord, IntentStatus
from redteam.orchestration.operator_intents.service import OperatorIntentService
from redteam.orchestration.operator_intents.store import SQLiteOperatorIntentStore
from redteam.orchestration.outbox import deliver_pending
from redteam.orchestration.paths.models import PathPlan, PathStep
from redteam.orchestration.paths.sqlite import SQLitePathStore
from redteam.orchestration.task_graph import SQLiteTaskGraphStore

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
RUN, MISSION, ENG = RunId("atomic-run"), MissionId("atomic-mission"), EngagementId("atomic-eng")


def stores(path, history_path):
    tasks, paths = SQLiteTaskGraphStore(path), SQLitePathStore(path)
    history = SQLiteOperatorIntentStore(history_path)
    svc = OperatorIntentService(store=history, task_graph_store=tasks, path_store=paths)
    publisher = EventPublisher(InMemoryEventBus(), run_id=RUN, engagement_id=ENG, source="test")
    return tasks, paths, history, svc, publisher


async def seed(path, history_path):
    tasks, paths, history, svc, publisher = stores(path, history_path)
    await tasks.create_mission(
        MissionPlan(
            mission_id=MISSION,
            engagement_id=ENG,
            objective="Assess authorization",
            success_criteria=["evidence-backed result"],
            status=MissionStatus.ACTIVE,
            created_at=NOW,
            updated_at=NOW,
            provenance=Provenance(source="operator"),
        )
    )
    for name in ("web", "mobile"):
        await tasks.create_task(
            MissionTask(
                task_id=TaskId(name),
                mission_id=MISSION,
                title=name,
                objective=f"Investigate {name}",
                capability_requirements=[name],
                status=TaskStatus.READY,
                created_at=NOW,
                updated_at=NOW,
                provenance=Provenance(source="planner"),
            )
        )
    await svc._coordinator.bind(str(RUN), str(MISSION), str(ENG))
    return tasks, paths, history, svc, publisher


async def records(coordinator):
    async def read(change):
        return copy.deepcopy(change.records)

    return await coordinator.change(read)


async def register(svc, publisher, instruction):
    value = interpret_instruction(instruction, mission_id=MISSION, now=NOW)
    record = IntentRecord(intent=value, correlation_id=str(value.intent_id), run_id=RUN)
    return await svc._register(record, publisher, None)


def _apply_process(path, history_path, intent_id):
    async def apply():
        _, _, _, svc, publisher = stores(path, history_path)
        result = await svc.apply(intent_id, publisher=publisher, run_id=RUN, now=NOW)
        return result.model_dump(mode="json")

    return asyncio.run(apply())


async def process_apply(path, history_path, ids):
    loop = asyncio.get_running_loop()
    with ProcessPoolExecutor(max_workers=2) as pool:
        return await asyncio.gather(
            *(
                loop.run_in_executor(
                    pool, _apply_process, str(path), str(history_path), str(identity)
                )
                for identity in ids
            )
        )


async def test_two_processes_apply_same_intent_once(tmp_path):
    path, audit_path = tmp_path / "tasks.sqlite3", tmp_path / "audit.sqlite3"
    tasks, _, history, svc, publisher = await seed(path, audit_path)
    intent = await register(svc, publisher, "focus web")
    results = await process_apply(path, audit_path, [intent.intent_id, intent.intent_id])
    assert all(result["applied"] for result in results)
    assert results[0]["applied_event_id"] == results[1]["applied_event_id"]
    mission = await tasks.get_mission(MISSION)
    assert mission.revision == 2 and mission.operator_preferences["focus_areas"] == ["web"]
    assert (await tasks.get_task(TaskId("web"))).priority == 500
    current = await records(svc._coordinator)
    assert current[("generation", f"{RUN}:{MISSION}")] == 1
    assert current[("intent", str(intent.intent_id))]["status"] == "applied"
    assert sum(kind == "outbox" for kind, _ in current) == 3
    assert (await history.get(intent.intent_id)).status is IntentStatus.APPLIED


async def test_two_processes_apply_distinct_intents_without_lost_updates(tmp_path):
    path, audit_path = tmp_path / "tasks.sqlite3", tmp_path / "audit.sqlite3"
    tasks, _, _, svc, publisher = await seed(path, audit_path)
    first = await register(svc, publisher, "focus web")
    second = await register(svc, publisher, "focus mobile")
    results = await process_apply(path, audit_path, [first.intent_id, second.intent_id])
    assert all(result["applied"] for result in results)
    mission = await tasks.get_mission(MISSION)
    assert mission.revision == 3
    assert set(mission.operator_preferences["focus_areas"]) == {"web", "mobile"}
    assert all(task.priority == 500 for task in await tasks.list_tasks())
    current = await records(svc._coordinator)
    assert current[("generation", f"{RUN}:{MISSION}")] == 2
    assert sum(kind == "outbox" for kind, _ in current) == 6


async def test_task_path_canonical_result_outbox_roll_back_together(tmp_path):
    path, audit_path = tmp_path / "tasks.sqlite3", tmp_path / "audit.sqlite3"
    tasks, paths, _, svc, publisher = await seed(path, audit_path)
    await paths.register(
        PathPlan(
            path_id="path",
            run_id=str(RUN),
            mission_id=str(MISSION),
            version=1,
            status="active",
            goal="Assess authorization",
            reason="investigate",
            steps=[PathStep(step_id="step", objective="collect evidence")],
        )
    )
    before = await records(svc._coordinator)

    async def partial_then_fail(change):
        await change.tasks.update_priority(TaskId("web"), 999, now=NOW)
        await change.paths.update_step(
            str(RUN), "path", 1, "step", status="blocked", outcome="waiting"
        )
        change.records[("intent", "failed-change")] = {"status": "applied"}
        change.queue(publisher.build(EventType.OPERATOR_INTENT_APPLIED, {}))
        raise ValueError("reject whole operation")

    with pytest.raises(ValueError, match="reject whole"):
        await svc._coordinator.change(partial_then_fail)
    assert (await tasks.get_task(TaskId("web"))).priority == 0
    assert (await paths.get_active(str(RUN))).steps[0].status == "pending"
    assert await records(svc._coordinator) == before


async def test_closed_run_replays_saved_envelopes_without_reapplying_stop(tmp_path):
    path, audit_path = tmp_path / "tasks.sqlite3", tmp_path / "audit.sqlite3"
    tasks, _, _, svc, publisher = await seed(path, audit_path)
    stop = await register(svc, publisher, "stop")
    applied = await svc.apply(stop.intent_id, publisher=publisher, run_id=RUN, now=NOW)
    assert applied.applied
    current = await records(svc._coordinator)
    saved_ids = {identity for (kind, identity) in current if kind == "outbox"}
    assert current[("binding", str(RUN))]["closed"]

    class FailsAfterAcceptance:
        def __init__(self):
            self.accepted = []

        async def publish(self, envelope):
            self.accepted.append(envelope)
            raise RuntimeError("temporary transport failure")

    failing = FailsAfterAcceptance()
    assert await deliver_pending(
        svc._coordinator, run_id=str(RUN), engagement_id=str(ENG), event_bus=failing
    ) == len(saved_ids)
    assert failing.accepted

    async def expire(change):
        for (kind, _), row in change.records.items():
            if kind == "outbox":
                row["lease_until"] = 0

    await svc._coordinator.change(expire)
    bus = InMemoryEventBus()
    assert (
        await deliver_pending(
            svc._coordinator, run_id=str(RUN), engagement_id=str(ENG), event_bus=bus
        )
        == 0
    )
    assert {str(event.event_id) for event in bus.history()} == saved_ids
    assert failing.accepted[0].event_id in {event.event_id for event in bus.history()}
    _, _, _, replay, replay_publisher = stores(path, audit_path)
    result = await replay.apply(stop, publisher=replay_publisher, run_id=RUN, now=NOW)
    assert result.applied_event_id == applied.applied_event_id
    assert (await tasks.get_mission(MISSION)).revision == 2
    assert all(task.status is TaskStatus.CANCELLED for task in await tasks.list_tasks())
    assert len([key for key in await records(replay._coordinator) if key[0] == "outbox"]) == len(
        saved_ids
    )


async def test_legacy_terminal_history_import_creates_no_effect_or_old_events(tmp_path):
    path, audit_path = tmp_path / "tasks.sqlite3", tmp_path / "separate-audit.sqlite3"
    tasks, _, history, svc, publisher = await seed(path, audit_path)
    value = interpret_instruction("focus web", mission_id=MISSION, now=NOW)
    legacy = IntentRecord(
        intent=value,
        correlation_id=str(value.intent_id),
        run_id=RUN,
        status=IntentStatus.APPLIED,
        outcome="historical focus",
        applied_at=NOW,
    )
    await history.append(legacy)
    await svc.process_pending(mission_id=MISSION, run_id=RUN, publisher=publisher, now=NOW)
    current = await records(svc._coordinator)
    assert current[("intent", str(legacy.intent_id))]["outcome"] == "historical focus"
    assert not any(kind in {"generation", "outbox"} for kind, _ in current)
    assert (await tasks.get_mission(MISSION)).revision == 1
    assert (await tasks.get_task(TaskId("web"))).priority == 0


async def test_concurrent_delivery_claims_once_without_holding_database_lock(tmp_path):
    path, audit_path = tmp_path / "tasks.sqlite3", tmp_path / "audit.sqlite3"
    _, _, _, svc, publisher = await seed(path, audit_path)
    intent = await register(svc, publisher, "focus web")
    await svc.apply(intent.intent_id, publisher=publisher, run_id=RUN, now=NOW)
    _, _, _, other, _ = stores(path, audit_path)
    started, release = asyncio.Event(), asyncio.Event()

    class SlowBus:
        def __init__(self):
            self.events = []

        async def publish(self, envelope):
            self.events.append(envelope)
            started.set()
            await release.wait()

    slow, second = SlowBus(), InMemoryEventBus()
    delivery = asyncio.create_task(
        deliver_pending(
            svc._coordinator,
            run_id=str(RUN),
            engagement_id=str(ENG),
            event_bus=slow,
        )
    )
    await asyncio.wait_for(started.wait(), timeout=2)
    # A second independent connection can read and commit while publication waits.
    pending = await deliver_pending(
        other._coordinator, run_id=str(RUN), engagement_id=str(ENG), event_bus=second
    )
    assert pending == 3
    assert second.history() == []
    current = await records(other._coordinator)
    owners = {row["owner"] for (kind, _), row in current.items() if kind == "outbox"}
    assert len(owners) == 1 and None not in owners
    release.set()
    assert await delivery == 0
    assert len(slow.events) == 3
