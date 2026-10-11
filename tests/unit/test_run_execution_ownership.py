"""Backend ownership is independent of Task claims and tool execution receipts."""

import asyncio
import hashlib
import os
import sys
from datetime import timedelta

import pytest

from redteam.core.contracts.llm_policy import LLMRequestPolicy
from redteam.orchestration.coordination import CoordinationError, Coordinator, coordinator_for
from redteam.orchestration.llm_requests import RunRequestLedger
from redteam.orchestration.paths.sqlite import SQLitePathStore
from redteam.orchestration.task_graph.sqlite import SQLiteTaskGraphStore
from redteam.orchestration.work.sqlite import SQLiteWorkStore
from tests.unit.test_orchestration_coordination import RUN, world
from tests.unit.test_orchestration_work import NOW
from tests.unit.test_resource_conflicts import resource_work


def sqlite_coordinator(path):
    return coordinator_for(SQLiteTaskGraphStore(path), SQLitePathStore(path))


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
async def test_ownership_is_nonblocking_and_allows_other_runs_and_state_changes(tmp_path, backend):
    w = await world(tmp_path, backend)
    other = sqlite_coordinator(w.db) if backend == "sqlite" else Coordinator(w.coordinator.backend)
    async with w.coordinator.exclusive_run(RUN):
        with pytest.raises(CoordinationError, match="^run_execution_already_owned$"):
            async with other.exclusive_run(RUN):
                pytest.fail("same Run cannot have a second driver")
        async with other.exclusive_run("independent-run"):
            async def change(tx):
                tx.records[("test", "state")] = {"allowed": True}
            await asyncio.wait_for(other.change(change), timeout=2)
    async with other.exclusive_run(RUN):
        pass


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
async def test_exception_and_cancellation_release_only_process_ownership(tmp_path, backend):
    w = await world(tmp_path, backend)
    entered = asyncio.Event()

    async def own():
        async with w.coordinator.exclusive_run(RUN):
            entered.set()
            await asyncio.Event().wait()

    actor = asyncio.create_task(own())
    await asyncio.wait_for(entered.wait(), timeout=2)
    actor.cancel()
    with pytest.raises(asyncio.CancelledError):
        await actor
    with pytest.raises(ValueError, match="synthetic failure"):
        async with w.coordinator.exclusive_run(RUN):
            raise ValueError("synthetic failure")
    async with w.coordinator.exclusive_run(RUN):
        assert len(await w.tasks.list_tasks()) == 2


async def test_sqlite_canonical_symlink_path_has_the_same_owner(tmp_path):
    w = await world(tmp_path, "sqlite")
    alias = tmp_path / "database-alias.sqlite"
    alias.symlink_to(w.db)
    async with w.coordinator.exclusive_run(RUN):
        with pytest.raises(CoordinationError, match="^run_execution_already_owned$"):
            async with sqlite_coordinator(alias).exclusive_run(RUN):
                pytest.fail("path spelling must not fork ownership")


@pytest.mark.parametrize("unsafe", ["symlink", "hardlink", "public", "directory", "fifo"])
async def test_sqlite_rejects_unsafe_companion_inodes(tmp_path, unsafe):
    w = await world(tmp_path, "sqlite")
    key = hashlib.sha256(RUN.encode()).hexdigest()
    lock = w.db.with_name(f".{w.db.name}.run-{key}.lock")
    target = tmp_path / "unrelated-file"
    target.write_bytes(b"unchanged")
    target.chmod(0o600)
    if unsafe == "symlink":
        lock.symlink_to(target)
    elif unsafe == "hardlink":
        os.link(target, lock)
    elif unsafe == "public":
        lock.write_bytes(b"")
        lock.chmod(0o644)
    elif unsafe == "directory":
        lock.mkdir()
    else:
        os.mkfifo(lock, mode=0o600)
    with pytest.raises(CoordinationError, match="^run_execution_storage_(unsafe|unavailable)$"):
        async with w.coordinator.exclusive_run(RUN):
            pytest.fail("unsafe storage cannot confer ownership")
    assert target.read_bytes() == b"unchanged"


async def test_companion_inode_is_retained_between_owners(tmp_path):
    w = await world(tmp_path, "sqlite")
    async with w.coordinator.exclusive_run(RUN):
        (lock,) = tmp_path.glob(".*.lock")
        before = lock.stat()
        assert before.st_mode & 0o077 == 0
    async with sqlite_coordinator(w.db).exclusive_run(RUN):
        assert lock.stat().st_ino == before.st_ino


async def test_custom_backend_without_ownership_fails_closed_only_when_requested():
    class CustomBackend:
        async def change(self, callback):
            return "ordinary-mode-ok"

    coordinator = Coordinator(CustomBackend())
    assert await coordinator.change(lambda _: None) == "ordinary-mode-ok"
    with pytest.raises(CoordinationError, match="^run_execution_backend_unsupported$"):
        coordinator.exclusive_run(RUN)
    with pytest.raises(CoordinationError, match="^run_execution_identity_missing$"):
        coordinator.exclusive_run("")


async def test_unsupported_platform_does_not_silently_use_memory_ownership(tmp_path, monkeypatch):
    w = await world(tmp_path, "sqlite")
    monkeypatch.setattr("redteam.orchestration.state_backend.os.name", "nt")
    with pytest.raises(CoordinationError, match="^run_execution_backend_unsupported$"):
        async with w.coordinator.exclusive_run(RUN):
            pytest.fail("durable mode must not downgrade to a process-local guard")


# Separate PID, not merely another facade or SQLite connection. This process
# performs no tool/LLM calls and exits on stdin EOF (or a test-issued signal).
_OWNER = """
import asyncio, sys
from redteam.orchestration.coordination import coordinator_for
from redteam.orchestration.paths.sqlite import SQLitePathStore
from redteam.orchestration.task_graph.sqlite import SQLiteTaskGraphStore
async def main():
    path, run = sys.argv[1:]
    owner = coordinator_for(SQLiteTaskGraphStore(path), SQLitePathStore(path))
    async with owner.exclusive_run(run):
        print('owned', flush=True)
        sys.stdin.buffer.readline()
asyncio.run(main())
"""


@pytest.mark.parametrize("ending", ["normal", "terminated", "killed"])
async def test_different_pid_ownership_and_exit_do_not_reset_unknown_accounting(tmp_path, ending):
    w = await world(tmp_path, "sqlite")
    ledger = RunRequestLedger(w.coordinator)
    await ledger.bind(RUN, LLMRequestPolicy(mode="bounded", max_requests=2))
    request = await ledger.reserve(RUN, owner="original-instance", kind="model")
    await ledger.dispatch(RUN, request)
    work = SQLiteWorkStore(tmp_path / "work.sqlite")
    await work.add(resource_work("original", run=RUN))
    await work.add(resource_work("contender", run="other-run"))
    await work.acquire("original", owner="original-instance", now=NOW,
                       ttl=timedelta(seconds=1))
    assert await work.mark_execution_started("original", owner="original-instance", now=NOW)
    before = await ledger.snapshot(RUN)
    child = await asyncio.create_subprocess_exec(
        sys.executable, "-c", _OWNER, str(w.db), RUN,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        assert child.stdout is not None
        assert await asyncio.wait_for(child.stdout.readline(), timeout=5) == b"owned\n"
        assert child.pid != os.getpid()
        with pytest.raises(CoordinationError, match="^run_execution_already_owned$"):
            async with w.coordinator.exclusive_run(RUN):
                pytest.fail("other process already owns this Run")
        if ending == "normal":
            assert child.stdin is not None
            child.stdin.close()
        elif ending == "terminated":
            child.terminate()
        else:
            child.kill()
        await asyncio.wait_for(child.wait(), timeout=5)
        async with sqlite_coordinator(w.db).exclusive_run(RUN):
            assert await ledger.snapshot(RUN) == before
            # TTL expiry/process exit is not an execution receipt.
            assert await work.acquire("contender", owner="other-instance",
                                      now=NOW + timedelta(seconds=2),
                                      ttl=timedelta(seconds=1)) is None
            assert (await work.get("original")).resource_execution_pending
    finally:
        if child.returncode is None:
            child.kill()
        await asyncio.wait_for(child.communicate(), timeout=5)
