"""Path writes share fresh SQLite state across instances and survive cancellation."""

from __future__ import annotations

import asyncio
import threading

import pytest

from redteam.orchestration.paths import PathPlan, PathStep, PathVersionConflict, SQLitePathStore


def _plan(version: int, *, parent: int | None = None) -> PathPlan:
    return PathPlan(
        run_id="run-a",
        path_id="path-a",
        version=version,
        parent_version=parent,
        status="active",
        goal="Authorized observation.",
        reason="Observe within the approved scope.",
        steps=[PathStep(step_id="step-a", objective="Observe the target.")],
    )


async def test_two_instances_preserve_concurrent_progress_evidence(tmp_path):
    path = tmp_path / "paths.sqlite3"
    first, second = SQLitePathStore(path), SQLitePathStore(path)
    await first.register(_plan(1))
    await asyncio.gather(
        *[
            (first if index % 2 else second).update_step(
                "run-a", "path-a", 1, "step-a", "running", evidence_ids=[f"evidence-{index}"]
            )
            for index in range(16)
        ]
    )
    latest = await second.get_active("run-a")
    assert latest is not None
    assert set(latest.steps[0].evidence_ids) == {f"evidence-{index}" for index in range(16)}
    # A second instance must carry the current progress into the new version.
    revised = await first.register(_plan(2, parent=1), expected_version=1)
    assert revised.steps[0].evidence_ids == latest.steps[0].evidence_ids


async def test_concurrent_revisions_use_latest_active_pointer(tmp_path):
    path = tmp_path / "paths.sqlite3"
    first, second = SQLitePathStore(path), SQLitePathStore(path)
    await first.register(_plan(1))
    results = await asyncio.gather(
        first.register(_plan(2, parent=1), expected_version=1),
        second.register(_plan(3, parent=1), expected_version=1),
        return_exceptions=True,
    )
    assert sum(isinstance(item, PathPlan) for item in results) == 1
    assert sum(isinstance(item, PathVersionConflict) for item in results) == 1
    assert len(await first.list_paths("run-a")) == 2


async def test_cancelled_update_waits_for_same_worker_commit(tmp_path, monkeypatch):
    path = tmp_path / "paths.sqlite3"
    store = SQLitePathStore(path)
    await store.register(_plan(1))
    entered, release = threading.Event(), threading.Event()
    original = store._save_memory
    calls = []

    def paused_save(db, memory):
        calls.append(1)
        entered.set()
        assert release.wait(5), "test failed to release the SQLite worker"
        original(db, memory)

    monkeypatch.setattr(store, "_save_memory", paused_save)
    pending = asyncio.create_task(
        store.update_step("run-a", "path-a", 1, "step-a", "completed", evidence_ids=["proof"])
    )
    assert await asyncio.to_thread(entered.wait, 5)
    pending.cancel()
    # Give the cancellation handler an event-loop turn; commit is still blocked.
    await asyncio.sleep(0)
    assert not pending.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert calls == [1]
    latest = await SQLitePathStore(path).get_active("run-a")
    assert latest is not None
    assert latest.steps[0].status == "completed"
    assert latest.steps[0].evidence_ids == ["proof"]


async def test_worker_save_failure_rolls_back_snapshot_progress_and_pointer(tmp_path, monkeypatch):
    path = tmp_path / "paths.sqlite3"
    store = SQLitePathStore(path)
    await store.register(_plan(1))
    original = store._save_memory

    def fail_after_writes(db, memory):
        original(db, memory)
        raise RuntimeError("injected transaction failure")

    monkeypatch.setattr(store, "_save_memory", fail_after_writes)
    with pytest.raises(RuntimeError, match="injected transaction failure"):
        await store.register(_plan(2, parent=1), expected_version=1)
    reopened = SQLitePathStore(path)
    latest = await reopened.get_active("run-a")
    assert latest is not None and latest.version == 1
    assert [item.version for item in await reopened.list_paths("run-a")] == [1]
