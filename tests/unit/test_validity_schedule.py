"""Observed deadlines are notification hints, not success or execution grants."""

import copy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from redteam.core.enums import MissionStatus
from redteam.orchestration.coordination import CoordinationError
from redteam.orchestration.evidence_validity import ValidityProjection, validity_schedule
from redteam.orchestration.state import RuntimeState
from tests.unit.test_orchestration_coordination import ENG, MISSION, RUN, world


async def scheduled(tmp_path, backend, *, active=True):
    w = await world(tmp_path, backend)
    deps = SimpleNamespace(coordinator=w.coordinator)
    runtime = RuntimeState(run_id=RUN, mission_id=MISSION, engagement_id=ENG)

    async def set_active(tx):
        tx.records[("specialist_lifecycle", RUN)] = {"state": "active"}
    if active:
        await w.coordinator.change(set_active)
    return w, deps, runtime


async def save(w, identity, *, status="valid", deadline=None, run_id=RUN):
    async def write(tx):
        tx.records[("evidence_validity_projection", f"{run_id}:{identity}")] = (
            ValidityProjection(evidence_id=identity, status=status, binding_ids=["binding"],
                               valid_until=deadline).model_dump(mode="json")
        )
    await w.coordinator.change(write)


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
async def test_only_current_run_explicit_valid_deadlines_schedule_wakeup(tmp_path, backend):
    w, deps, runtime = await scheduled(tmp_path, backend)
    now = datetime.now(timezone.utc)
    empty = await validity_schedule(deps, runtime)
    assert empty.next_expiry is None
    await save(w, "unknown")
    await save(w, "old", status="stale", deadline=now - timedelta(seconds=1))
    await save(w, "uncertain", status="inconclusive", deadline=now)
    await save(w, "other", deadline=now, run_id="another-run")
    assert (await validity_schedule(deps, runtime)).next_expiry is None
    await save(w, "later", deadline=now + timedelta(seconds=20))
    await save(w, "first", deadline=now + timedelta(seconds=10))
    first = await validity_schedule(deps, runtime)
    assert first.next_expiry == now + timedelta(seconds=10)
    assert await validity_schedule(deps, runtime) == first
    await save(w, "first", status="stale", deadline=first.next_expiry)
    second = await validity_schedule(deps, runtime)
    assert second.next_expiry == now + timedelta(seconds=20)
    assert second.digest != first.digest
    # A resumed active cohort observes an overdue *stored valid* deadline;
    # it must refresh originals, not quietly discard this timestamp.
    await save(w, "due", deadline=now - timedelta(seconds=1))
    assert (await validity_schedule(deps, runtime)).next_expiry < now


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
@pytest.mark.parametrize("blocked", [
    "missing", "held", "quiescent", "paused", "cancelled", "completed", "failed",
    "closed", "pending-stop",
])
async def test_inactive_or_pending_operator_state_never_schedules(tmp_path, backend, blocked):
    w, deps, runtime = await scheduled(tmp_path, backend, active=blocked != "missing")
    await save(w, "proof", deadline=datetime.now(timezone.utc))

    async def block(tx):
        key = ("specialist_lifecycle", RUN)
        if blocked == "missing":
            return
        elif blocked in {"held", "quiescent"}:
            tx.records[key] = {"state": blocked}
        elif blocked == "closed":
            tx.records[("binding", RUN)]["closed"] = True
        elif blocked == "pending-stop":
            tx.records[("intent", "stop")] = {
                "run_id": RUN, "intent": {"mission_id": MISSION, "intent_type": "stop"},
                "status": "received",
            }
        else:
            mission = await tx.tasks.get_mission(MISSION)
            await tx.tasks.update_mission(mission.model_copy(update={
                "status": MissionStatus(blocked), "revision": mission.revision + 1,
            }))
    await w.coordinator.change(block)
    async def snapshot(tx):
        return copy.deepcopy(tx.records), tx.tasks.snapshot_tasks()
    before = await w.coordinator.change(snapshot)
    assert await validity_schedule(deps, runtime) is None
    assert await w.coordinator.change(snapshot) == before


@pytest.mark.parametrize("bad", ["key", "timezone", "schema", "status", "scope"])
async def test_bad_bookkeeping_is_not_silently_no_expiry(tmp_path, bad):
    w, deps, runtime = await scheduled(tmp_path, "memory")
    await save(w, "proof", deadline=datetime.now(timezone.utc))
    async def corrupt(tx):
        row = tx.records[("evidence_validity_projection", f"{RUN}:proof")]
        if bad == "key":
            row["evidence_id"] = "different-proof"
        elif bad == "timezone":
            row["valid_until"] = "2030-01-01T00:00:00"
        elif bad == "schema":
            row["unknown_version"] = "2"
        elif bad == "status":
            row["status"] = "assumed_success"
        else:
            tx.records[("binding", RUN)]["engagement_id"] = "another-engagement"
    await w.coordinator.change(corrupt)
    with pytest.raises((ValueError, CoordinationError)):
        await validity_schedule(deps, runtime)
