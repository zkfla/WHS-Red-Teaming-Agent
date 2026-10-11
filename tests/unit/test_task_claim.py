"""Instance-targeted task claims on the existing atomic boundary.

The claim is owner-field read -> ``update_task`` -> ``transition_task`` in
ONE ``coordinator.change(...)`` callback using only existing staged specs.
These tests prove the loser of a race rolls back (``StateConflict``) on
both backends, unowned RUNNING rows are adopted (legacy migration path),
and release is idempotent and never steals another instance's claim.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from redteam.core.contracts.mission import MissionPlan, MissionTask, Provenance
from redteam.core.enums import MissionStatus, TaskStatus
from redteam.core.ids import EngagementId, MissionId, TaskId
from redteam.orchestration.coordination import StateConflict, coordinator_for
from redteam.orchestration.task_graph.claim import (
    claim_mission_task,
    release_mission_task_claim,
)
from redteam.orchestration.task_graph.memory import InMemoryTaskGraphStore
from redteam.orchestration.task_graph.sqlite import SQLiteTaskGraphStore

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
OWNER_A = "run-1:instance:a"
OWNER_B = "run-1:instance:b"


def mission() -> MissionPlan:
    return MissionPlan(
        mission_id=MissionId("mission-1"),
        engagement_id=EngagementId("eng-1"),
        objective="Assess authorization",
        priorities=["authorization"],
        success_criteria=["evidence-backed result"],
        initial_workstreams=["API assessment"],
        assumptions=["test tenant available"],
        operator_constraints={"credential_ref": "vault://eng/tester"},
        operator_preferences={"focus": "API"},
        status=MissionStatus.ACTIVE,
        revision=1,
        created_at=NOW,
        updated_at=NOW,
        provenance=Provenance(source="operator"),
    )


def task(task_id: str, *, status: TaskStatus = TaskStatus.PENDING) -> MissionTask:
    return MissionTask(
        task_id=TaskId(task_id),
        mission_id=MissionId("mission-1"),
        title=task_id,
        objective=f"perform {task_id}",
        status=status,
        provenance=Provenance(source="planner"),
        created_at=NOW,
        updated_at=NOW,
    )


@pytest.fixture(params=["memory", "sqlite"])
def world(request, tmp_path):
    if request.param == "memory":
        store = InMemoryTaskGraphStore()
    else:
        store = SQLiteTaskGraphStore(tmp_path / "task-graph.sqlite3")
    return store, coordinator_for(store, None)


async def _ready_task(store, task_id: str = "t1") -> None:
    await store.create_mission(mission())
    await store.create_task(task(task_id))
    await store.transition_task(TaskId(task_id), TaskStatus.READY, now=NOW)


async def test_claim_ready_sets_owner_and_running(world) -> None:
    store, coordinator = world
    await _ready_task(store)

    async def claim(change):
        return await claim_mission_task(change, TaskId("t1"), owner=OWNER_A, now=NOW)

    claimed = await coordinator.change(claim)
    assert claimed.status is TaskStatus.RUNNING
    assert claimed.occupant_owner == OWNER_A
    stored = await store.get_task(TaskId("t1"))
    assert stored.status is TaskStatus.RUNNING
    assert stored.occupant_owner == OWNER_A


async def test_claim_race_second_owner_rejected(world) -> None:
    store, coordinator = world
    await _ready_task(store)

    async def claim(owner: str):
        async def _claim(change):
            return await claim_mission_task(change, TaskId("t1"), owner=owner, now=NOW)

        return await coordinator.change(_claim)

    first = await claim(OWNER_A)
    assert first.occupant_owner == OWNER_A
    with pytest.raises(StateConflict):
        await claim(OWNER_B)
    stored = await store.get_task(TaskId("t1"))
    assert stored.status is TaskStatus.RUNNING
    assert stored.occupant_owner == OWNER_A


async def test_concurrent_claims_single_winner(world) -> None:
    store, coordinator = world
    await _ready_task(store)

    async def claim(owner: str):
        async def _claim(change):
            return await claim_mission_task(change, TaskId("t1"), owner=owner, now=NOW)

        return await coordinator.change(_claim)

    results = await asyncio.gather(
        claim(OWNER_A), claim(OWNER_B), return_exceptions=True
    )
    winners = [item for item in results if not isinstance(item, BaseException)]
    losers = [item for item in results if isinstance(item, BaseException)]
    assert len(winners) == 1
    assert len(losers) == 1
    assert isinstance(losers[0], StateConflict)
    stored = await store.get_task(TaskId("t1"))
    assert stored.status is TaskStatus.RUNNING
    assert stored.occupant_owner == winners[0].occupant_owner
    assert stored.occupant_owner in {OWNER_A, OWNER_B}


async def test_adopt_running_unowned_legacy_row(world) -> None:
    store, coordinator = world
    await store.create_mission(mission())
    await store.create_task(task("legacy", status=TaskStatus.RUNNING))
    assert (await store.get_task(TaskId("legacy"))).occupant_owner is None

    async def claim(change):
        return await claim_mission_task(change, TaskId("legacy"), owner=OWNER_A, now=NOW)

    claimed = await coordinator.change(claim)
    assert claimed.status is TaskStatus.RUNNING
    assert claimed.occupant_owner == OWNER_A


async def test_pending_task_not_claimable(world) -> None:
    store, coordinator = world
    await store.create_mission(mission())
    await store.create_task(task("t1"))

    async def claim(change):
        return await claim_mission_task(change, TaskId("t1"), owner=OWNER_A, now=NOW)

    with pytest.raises(StateConflict):
        await coordinator.change(claim)
    assert (await store.get_task(TaskId("t1"))).occupant_owner is None


async def test_release_clears_only_owner(world) -> None:
    store, coordinator = world
    await _ready_task(store)

    async def change_owner(owner: str, *, release: bool = False):
        async def _change(change):
            if release:
                return await release_mission_task_claim(
                    change, TaskId("t1"), owner=owner, now=NOW
                )
            return await claim_mission_task(change, TaskId("t1"), owner=owner, now=NOW)

        return await coordinator.change(_change)

    await change_owner(OWNER_A)
    # Another instance's release never steals the claim.
    kept = await change_owner(OWNER_B, release=True)
    assert kept.occupant_owner == OWNER_A
    released = await change_owner(OWNER_A, release=True)
    assert released.occupant_owner is None
    assert released.status is TaskStatus.RUNNING
    # Idempotent: releasing again or releasing a missing task is a no-op.
    rereleased = await change_owner(OWNER_A, release=True)
    assert rereleased.occupant_owner is None

    async def release_missing(change):
        return await release_mission_task_claim(
            change, TaskId("nope"), owner=OWNER_A, now=NOW
        )

    assert await coordinator.change(release_missing) is None


async def test_occupant_survives_status_transition(world) -> None:
    store, coordinator = world
    await _ready_task(store)

    async def claim(change):
        return await claim_mission_task(change, TaskId("t1"), owner=OWNER_A, now=NOW)

    await coordinator.change(claim)
    completed = await store.transition_task(TaskId("t1"), TaskStatus.COMPLETED, now=NOW)
    assert completed.occupant_owner == OWNER_A
