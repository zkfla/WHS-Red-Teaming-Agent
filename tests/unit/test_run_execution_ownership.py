"""Central Run ownership: companion handles never reset durable execution accounting."""

import asyncio
import hashlib
import os
import sys

import pytest

from redteam.core.contracts.llm_policy import LLMRequestPolicy
from redteam.orchestration.coordination import CoordinationError
from redteam.orchestration.state_backend import run_storage_ownership
from tests.unit.test_central_state import RUN, world


async def test_ownership_is_nonblocking_and_other_runs_can_change_state(tmp_path):
    state = await world(tmp_path)
    async with state.exclusive_run(RUN):
        with pytest.raises(CoordinationError, match="run_execution_already_owned"):
            with run_storage_ownership(state.path, RUN):
                pytest.fail("second driver entered the same Run")
        with run_storage_ownership(state.path, "another-run"):
            assert len(await state.list_tasks()) == 0
    async with state.exclusive_run(RUN):
        pass


async def test_exception_and_cancellation_release_ownership(tmp_path):
    state = await world(tmp_path)
    entered = asyncio.Event()

    async def own():
        async with state.exclusive_run(RUN):
            entered.set()
            await asyncio.Event().wait()

    actor = asyncio.create_task(own())
    await asyncio.wait_for(entered.wait(), 2)
    actor.cancel()
    with pytest.raises(asyncio.CancelledError):
        await actor
    with pytest.raises(ValueError):
        async with state.exclusive_run(RUN):
            raise ValueError("synthetic failure")
    async with state.exclusive_run(RUN):
        pass


@pytest.mark.skipif(os.name != "posix", reason="POSIX path and inode validation")
async def test_symlink_database_spelling_has_the_same_owner(tmp_path):
    state = await world(tmp_path)
    alias = tmp_path / "alias.db"
    alias.symlink_to(state.path)
    async with state.exclusive_run(RUN):
        with pytest.raises(CoordinationError, match="run_execution_already_owned"):
            with run_storage_ownership(alias, RUN):
                pytest.fail("alias forked ownership")


@pytest.mark.parametrize("unsafe", ["symlink", "hardlink", "public", "directory", "fifo"])
@pytest.mark.skipif(os.name != "posix", reason="POSIX inode and permission contract")
async def test_unsafe_companion_is_refused(tmp_path, unsafe):
    state = await world(tmp_path)
    key = hashlib.sha256(RUN.encode()).hexdigest()
    lock = state.path.with_name(f".{state.path.name}.run-{key}.lock")
    target = tmp_path / "untouched"
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
    with pytest.raises(CoordinationError, match="run_execution_storage_(unsafe|unavailable)"):
        async with state.exclusive_run(RUN):
            pytest.fail("unsafe companion accepted")
    assert target.read_bytes() == b"unchanged"


async def test_readonly_missing_companion_creates_no_file(tmp_path):
    with run_storage_ownership(tmp_path / "absent.db", RUN, create=False):
        assert list(tmp_path.iterdir()) == []


def test_windows_unmarked_storage_and_retired_source_fence(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from redteam import sqlite_support

    source = tmp_path / "state.db"
    marker = sqlite_support.retirement_path(source)
    monkeypatch.setattr(sqlite_support, "os", SimpleNamespace(name="nt"))
    assert sqlite_support.read_storage_retirement(source) is None
    assert list(tmp_path.iterdir()) == []
    marker.write_text("retired source", encoding="utf-8")
    with pytest.raises(sqlite_support.StorageRetiredError,
                       match="migration_retirement_marker_unsupported_platform"):
        sqlite_support.check_storage_writable(source)
    assert marker.read_text(encoding="utf-8") == "retired source"


async def test_companion_inode_is_retained_between_owners(tmp_path):
    state = await world(tmp_path)
    async with state.exclusive_run(RUN):
        (lock,) = tmp_path.glob(".*.lock")
        inode = lock.stat().st_ino
    async with state.exclusive_run(RUN):
        assert lock.stat().st_ino == inode


@pytest.mark.skipif(os.name != "nt", reason="Windows sharing and casing contract")
async def test_windows_live_companion_cannot_be_removed_or_replaced(tmp_path):
    state = await world(tmp_path)
    async with state.exclusive_run(RUN):
        (lock,) = tmp_path.glob(".*.lock")
        with pytest.raises(PermissionError):
            lock.unlink()
        with pytest.raises(PermissionError):
            lock.rename(tmp_path / "replacement.lock")
        with pytest.raises(CoordinationError, match="run_execution_already_owned"):
            with run_storage_ownership(str(state.path).upper(), RUN):
                pytest.fail("case change forked ownership")


@pytest.mark.skipif(os.name != "nt", reason="Windows companion validation")
@pytest.mark.parametrize("unsafe", ["hardlink", "directory", "symlink"])
async def test_windows_rejects_unsafe_companion_without_changing_target(tmp_path, unsafe):
    state = await world(tmp_path)
    key = hashlib.sha256(RUN.encode()).hexdigest()
    lock = state.path.with_name(f".{state.path.name}.run-{key}.lock")
    target = tmp_path / "untouched"
    target.write_bytes(b"unchanged")
    if unsafe == "hardlink":
        os.link(target, lock)
    elif unsafe == "directory":
        lock.mkdir()
    else:
        try:
            lock.symlink_to(target)
        except OSError as exc:
            if exc.winerror == 1314:
                pytest.skip("symlink privilege unavailable")
            raise
    with pytest.raises(CoordinationError, match="run_execution_storage_(unsafe|unavailable)"):
        async with state.exclusive_run(RUN):
            pytest.fail("unsafe companion accepted")
    assert target.read_bytes() == b"unchanged"


_OWNER = """
import sys
from redteam.orchestration.state_backend import run_storage_ownership
with run_storage_ownership(sys.argv[1], sys.argv[2]):
    print('owned', flush=True)
    sys.stdin.buffer.readline()
"""


@pytest.mark.parametrize("ending", ["normal", "terminated", "killed"])
async def test_process_exit_does_not_reset_unknown_request_accounting(tmp_path, ending):
    state = await world(tmp_path)
    ledger = state.request_ledger()
    await ledger.bind(RUN, LLMRequestPolicy(mode="bounded", max_requests=2))
    request = await ledger.reserve(RUN, owner="instance", kind="model")
    await ledger.dispatch(RUN, request)
    before = await ledger.snapshot(RUN)
    child = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        _OWNER,
        str(state.path),
        RUN,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        assert child.stdout is not None
        assert (await asyncio.wait_for(child.stdout.readline(), 30)).strip() == b"owned"
        with pytest.raises(CoordinationError, match="run_execution_already_owned"):
            async with state.exclusive_run(RUN):
                pytest.fail("another process owns the Run")
        if ending == "normal":
            assert child.stdin is not None
            child.stdin.close()
        elif ending == "terminated":
            child.terminate()
        else:
            child.kill()
        await asyncio.wait_for(child.wait(), 5)
        async with state.exclusive_run(RUN):
            assert await ledger.snapshot(RUN) == before
    finally:
        if child.returncode is None:
            child.kill()
        await asyncio.wait_for(child.communicate(), 5)
