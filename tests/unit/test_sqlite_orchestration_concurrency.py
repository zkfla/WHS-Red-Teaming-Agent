"""Independent connections preserve rows and settle started work before cancellation."""

import asyncio
import sqlite3
import threading
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timezone

import pytest

from redteam.core.contracts.mission import MissionPlan, MissionTask, Provenance
from redteam.core.enums import MissionStatus
from redteam.core.ids import EngagementId, MissionId, TaskId
from redteam.orchestration.operator_intents.interpreter import interpret_instruction
from redteam.orchestration.operator_intents.models import IntentRecord, IntentStatus
from redteam.orchestration.operator_intents.store import (
    IntentIdentityConflictError,
    SQLiteOperatorIntentStore,
)
from redteam.orchestration.sqlite_support import run_transaction
from redteam.orchestration.task_graph import SQLiteTaskGraphStore
from redteam.orchestration.task_graph.rules import DuplicateTaskError

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
MISSION = MissionId("sqlite-mission")


def mission() -> MissionPlan:
    return MissionPlan(
        mission_id=MISSION,
        engagement_id=EngagementId("sqlite-engagement"),
        objective="Assess authorization",
        success_criteria=["evidence-backed result"],
        status=MissionStatus.ACTIVE,
        created_at=NOW,
        updated_at=NOW,
        provenance=Provenance(source="operator"),
    )


def task(name: str, *, parent: str | None = None) -> MissionTask:
    return MissionTask(
        task_id=TaskId(name),
        mission_id=MISSION,
        title=name,
        objective=name,
        parent_task_id=TaskId(parent) if parent else None,
        created_at=NOW,
        updated_at=NOW,
        provenance=Provenance(source="planner"),
    )


def intent(instruction: str = "focus web") -> IntentRecord:
    value = interpret_instruction(instruction, mission_id=MISSION, now=NOW)
    return IntentRecord(intent=value, correlation_id=str(value.intent_id))


async def test_two_task_stores_read_latest_rows_without_erasing_other_writes(tmp_path):
    path = tmp_path / "tasks.sqlite3"
    first, second = SQLiteTaskGraphStore(path), SQLiteTaskGraphStore(path)
    await asyncio.gather(first.setup(), second.setup())
    await first.create_mission(mission())
    await first.create_task(task("first"))
    await second.create_task(task("second"))
    assert {str(value.task_id) for value in await first.list_tasks()} == {"first", "second"}
    await second.update_mission(mission().model_copy(update={"revision": 2}))
    assert (await first.get_mission(MISSION)).revision == 2
    with pytest.raises(ValueError, match="revision"):
        await first.update_mission(mission().model_copy(update={"revision": 2}))


async def test_split_failure_rolls_back_all_children(tmp_path):
    store = SQLiteTaskGraphStore(tmp_path / "tasks.sqlite3")
    await store.create_mission(mission())
    await store.create_task(task("parent"))
    children = [task("child", parent="parent"), task("child", parent="parent")]
    with pytest.raises(DuplicateTaskError):
        await store.split_task(TaskId("parent"), children)
    assert await store.get_task(TaskId("child")) is None
    assert [str(value.task_id) for value in await store.list_tasks()] == ["parent"]


def _create_in_process(path: str, name: str) -> None:
    asyncio.run(SQLiteTaskGraphStore(path).create_task(task(name)))


async def test_separate_process_task_writes_keep_all_rows(tmp_path):
    path = tmp_path / "tasks.sqlite3"
    store = SQLiteTaskGraphStore(path)
    await store.create_mission(mission())
    loop = asyncio.get_running_loop()
    with ProcessPoolExecutor(max_workers=2) as pool:
        await asyncio.gather(
            loop.run_in_executor(pool, _create_in_process, str(path), "one"),
            loop.run_in_executor(pool, _create_in_process, str(path), "two"),
        )
    assert {str(value.task_id) for value in await store.list_tasks()} == {"one", "two"}


async def test_intent_projection_preserves_other_rows_and_terminal_outcome(tmp_path):
    path = tmp_path / "intents.sqlite3"
    first, second = SQLiteOperatorIntentStore(path), SQLiteOperatorIntentStore(path)
    await asyncio.gather(first.setup(), second.setup())
    original, other = intent(), intent("pause")
    await first.append(original)
    await second.append(other)
    applied = original.model_copy(update={"status": IntentStatus.APPLIED, "outcome": "focused"})
    await first.update(applied)
    await second.update(original)
    assert await second.get(original.intent_id) == applied
    assert len(await first.list_for_mission(MISSION)) == 2
    changed = original.model_copy(
        update={"intent": original.intent.model_copy(update={"instruction": "stop"})}
    )
    with pytest.raises(IntentIdentityConflictError):
        await second.update(changed)
    assert await first.get(original.intent_id) == applied


async def test_cancelled_awaiter_waits_for_same_worker_commit(tmp_path):
    path = tmp_path / "cancel.sqlite3"
    started, release = threading.Event(), threading.Event()
    calls = []

    def mutate(db):
        calls.append(1)
        db.execute("CREATE TABLE outcome(value INTEGER)")
        db.execute("INSERT INTO outcome VALUES(1)")
        started.set()
        assert release.wait(timeout=5)

    pending = asyncio.create_task(run_transaction(path, mutate, write=True))
    assert await asyncio.to_thread(started.wait, 5)
    pending.cancel()
    await asyncio.sleep(0)
    assert not pending.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert calls == [1]
    db = sqlite3.connect(path)
    try:
        assert db.execute("SELECT value FROM outcome").fetchall() == [(1,)]
    finally:
        db.close()


async def test_cancelled_awaiter_waits_for_worker_rollback(tmp_path):
    path = tmp_path / "rollback.sqlite3"
    await run_transaction(
        path, lambda db: db.execute("CREATE TABLE outcome(value INTEGER)"), write=True
    )
    started, release = threading.Event(), threading.Event()

    def mutate(db):
        db.execute("INSERT INTO outcome VALUES(1)")
        started.set()
        assert release.wait(timeout=5)
        raise ValueError("reject staged change")

    pending = asyncio.create_task(run_transaction(path, mutate, write=True))
    assert await asyncio.to_thread(started.wait, 5)
    pending.cancel()
    await asyncio.sleep(0)
    assert not pending.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await pending
    rows = await run_transaction(
        path, lambda db: db.execute("SELECT value FROM outcome").fetchall()
    )
    assert rows == []


async def test_retry_limit_and_thread_owned_connection(tmp_path):
    from redteam.orchestration.sqlite_support import run_sqlite

    calls = []

    def busy():
        calls.append(1)
        raise sqlite3.OperationalError("database is locked")

    with pytest.raises(sqlite3.OperationalError, match="locked"):
        await run_sqlite(busy)
    assert len(calls) == 3
    calls.clear()

    def invalid():
        calls.append(1)
        raise sqlite3.OperationalError("no such table: missing")

    with pytest.raises(sqlite3.OperationalError, match="no such table"):
        await run_sqlite(invalid)
    assert len(calls) == 1
    main_thread = threading.get_ident()

    def owned(db):
        assert threading.get_ident() != main_thread
        assert db.execute("PRAGMA busy_timeout").fetchone() == (1000,)
        assert db.execute("PRAGMA foreign_keys").fetchone() == (1,)

    await run_transaction(tmp_path / "worker.sqlite3", owned)
