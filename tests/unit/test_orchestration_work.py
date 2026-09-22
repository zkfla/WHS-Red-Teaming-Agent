"""Work ledger and scheduler ownership/concurrency semantics."""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from redteam.core.ids import AgentId, GraphNodeId, RunId
from redteam.orchestration.candidates.models import PriorityScore
from redteam.orchestration.work.memory import InMemoryWorkStore
from redteam.orchestration.work.models import WorkItem, WorkStatus
from redteam.orchestration.work.sqlite import SQLiteWorkStore

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def item(
    work_id: str,
    *,
    candidate: str | None = None,
    conflict: str | None = None,
    prerequisites: list[str] | None = None,
    priority: float = 1.0,
    repeatable: bool = False,
    run_id: str = "run-1",
) -> WorkItem:
    return WorkItem(
        work_id=work_id,
        run_id=RunId(run_id),
        candidate_key=candidate or f"candidate:{work_id}",
        agent_id=AgentId("agent"),
        target_node_ids=[GraphNodeId("host:a.local")],
        prerequisite_ids=prerequisites or [],
        conflict_keys=[conflict] if conflict else [],
        priority=PriorityScore(total=priority),
        repeatable=repeatable,
        created_at=NOW,
        updated_at=NOW,
    )


@pytest.fixture(params=["memory", "sqlite"])
def store(request, tmp_path):
    if request.param == "memory":
        return InMemoryWorkStore()
    return SQLiteWorkStore(tmp_path / "work.sqlite3")


async def test_only_one_active_copy_of_a_candidate_is_accepted(store) -> None:
    assert await store.add(item("w1", candidate="same"))
    assert not await store.add(item("w2", candidate="same"))


async def test_nonrepeatable_candidate_stays_deduplicated_after_completion(store) -> None:
    assert await store.add(item("w1", candidate="same"))
    assert await store.acquire("w1", owner="worker", now=NOW, ttl=timedelta(seconds=10))
    assert await store.complete("w1", owner="worker", status=WorkStatus.SUCCEEDED, now=NOW)

    assert not await store.add(item("w2", candidate="same"))
    assert await store.add(item("w3", candidate="same", repeatable=True))


async def test_candidate_history_and_listing_are_isolated_by_run(store) -> None:
    assert await store.add(item("w1", candidate="same", run_id="run-1"))
    assert await store.add(item("w2", candidate="same", run_id="run-2"))

    first = await store.list_items(run_id=RunId("run-1"))
    second = await store.list_items(run_id=RunId("run-2"))

    assert [work.work_id for work in first] == ["w1"]
    assert [work.work_id for work in second] == ["w2"]


async def test_sqlite_migrates_the_pre_run_isolation_schema(tmp_path) -> None:
    path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(path) as db:
        db.executescript(
            """
            CREATE TABLE work_items (
                work_id TEXT PRIMARY KEY,
                candidate_key TEXT NOT NULL,
                status TEXT NOT NULL,
                priority REAL NOT NULL,
                payload TEXT NOT NULL
            );
            CREATE UNIQUE INDEX idx_one_active_candidate
                ON work_items(candidate_key)
                WHERE status IN ('pending', 'leased', 'running');
            CREATE TABLE work_leases (
                work_id TEXT PRIMARY KEY REFERENCES work_items(work_id) ON DELETE CASCADE,
                owner TEXT NOT NULL,
                issued_at TEXT NOT NULL,
                heartbeat_at TEXT NOT NULL,
                expires_at TEXT NOT NULL
            );
            """
        )

    store = SQLiteWorkStore(path)
    assert await store.add(item("new", candidate="same", run_id="run-new"))
    assert [entry.work_id for entry in await store.list_items(run_id=RunId("run-new"))] == [
        "new"
    ]
    with sqlite3.connect(path) as db:
        indexes = {
            row[0]
            for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            ).fetchall()
        }
    assert "idx_work_run_status_priority" in indexes


async def test_lease_is_atomic_under_concurrency(store) -> None:
    await store.add(item("w1"))

    leases = await asyncio.gather(
        *[
            store.acquire("w1", owner=f"worker-{index}", now=NOW, ttl=timedelta(seconds=10))
            for index in range(8)
        ]
    )

    assert len([lease for lease in leases if lease is not None]) == 1


async def test_conflicting_resources_are_not_leased_together(store) -> None:
    await store.add(item("w1", conflict="resource:opaque-1", priority=2))
    await store.add(item("w2", conflict="resource:opaque-1", priority=1))
    assert await store.acquire("w1", owner="worker", now=NOW, ttl=timedelta(seconds=10))
    assert await store.acquire("w2", owner="worker", now=NOW, ttl=timedelta(seconds=10)) is None


async def test_dependency_requires_success_not_merely_completion(store) -> None:
    await store.add(item("parent"))
    await store.add(item("child", prerequisites=["parent"]))
    assert await store.acquire("child", owner="worker", now=NOW, ttl=timedelta(seconds=10)) is None
    assert await store.acquire("parent", owner="worker", now=NOW, ttl=timedelta(seconds=10))
    assert await store.complete("parent", owner="worker", status=WorkStatus.FAILED, now=NOW)
    assert await store.acquire("child", owner="worker", now=NOW, ttl=timedelta(seconds=10)) is None
    child = await store.get("child")
    assert child is not None and child.status is WorkStatus.BLOCKED
    assert child.outcome_reason == "prerequisite_not_succeeded:parent:failed"


async def test_expired_lease_becomes_stale_not_success(store) -> None:
    await store.add(item("w1"))
    assert await store.acquire("w1", owner="worker", now=NOW, ttl=timedelta(seconds=1))

    assert await store.reclaim_expired(now=NOW + timedelta(seconds=2)) == ["w1"]
    stored = await store.get("w1")
    assert stored is not None
    assert stored.status is WorkStatus.STALE
    assert stored.outcome_reason == "lease_expired"


async def test_heartbeat_extends_the_lease_for_both_backends(store) -> None:
    await store.add(item("w1"))
    assert await store.acquire("w1", owner="worker", now=NOW, ttl=timedelta(seconds=1))
    renewed = await store.heartbeat(
        "w1",
        owner="worker",
        now=NOW + timedelta(seconds=0.75),
        ttl=timedelta(seconds=1),
    )

    assert renewed is not None
    assert await store.reclaim_expired(now=NOW + timedelta(seconds=1.25)) == []
    assert await store.reclaim_expired(now=NOW + timedelta(seconds=2)) == ["w1"]


async def test_expired_owner_cannot_complete_without_an_explicit_reclaim(store) -> None:
    await store.add(item("w1"))
    assert await store.acquire("w1", owner="worker", now=NOW, ttl=timedelta(seconds=1))

    completed = await store.complete(
        "w1",
        owner="worker",
        status=WorkStatus.SUCCEEDED,
        now=NOW + timedelta(seconds=2),
    )

    assert completed is False
    stored = await store.get("w1")
    assert stored is not None and stored.status is WorkStatus.STALE
    assert stored.outcome_reason == "lease_expired"


async def test_abandon_run_closes_pending_and_owned_leased_work(store) -> None:
    await store.add(item("pending", run_id="run-1"))
    await store.add(item("leased", run_id="run-1"))
    await store.add(item("other", run_id="run-2"))
    assert await store.acquire(
        "leased", owner="run-1", now=NOW, ttl=timedelta(seconds=10)
    )

    abandoned = await store.abandon_run(
        RunId("run-1"),
        owner="run-1",
        now=NOW,
        reason="runner_error:RuntimeError",
    )

    assert abandoned == ["leased", "pending"]
    assert all(
        work.status is WorkStatus.FAILED
        for work in await store.list_items(run_id=RunId("run-1"))
    )
    assert (await store.get("other")).status is WorkStatus.PENDING
