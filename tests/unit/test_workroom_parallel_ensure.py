"""Different warm rooms overlap; same-room setup and global creation stay guarded."""

import asyncio

from redteam.capability.shell_workroom import container_name_for
from tests.unit.test_shell_adapter import _FakeDocker, _manager


async def _fixture_adoption(name, key):
    # This fixture supplies valid ownership for each independent fake room.
    # Real label/network validation has separate adoption regressions.
    assert name == container_name_for(key)


async def test_warm_workrooms_overlap_without_duplicate_same_key_inspection(monkeypatch):
    fake = _FakeDocker()
    manager = _manager(fake)
    first = await manager.ensure_workroom("parallel.first")
    second = await manager.ensure_workroom("parallel.second")
    monkeypatch.setattr(manager, "_verify_adoption", _fixture_adoption)
    original = manager._inspect_state
    entered = []
    both = asyncio.Event()
    release = asyncio.Event()

    async def inspect(name):
        entered.append(name)
        if len(entered) == 2:
            both.set()
        await release.wait()
        return await original(name)

    monkeypatch.setattr(manager, "_inspect_state", inspect)
    tasks = [asyncio.create_task(manager.ensure_workroom(key)) for key in (
        "parallel.first", "parallel.first", "parallel.second",
    )]
    try:
        await asyncio.wait_for(both.wait(), 2)
        assert sorted(entered) == sorted([first, second])
    finally:
        release.set()
        values = await asyncio.gather(*tasks)
    assert values == [first, first, second]
    assert entered.count(first) == 2
    assert len(fake.containers) == 2


async def test_same_key_cold_creation_is_single_and_cancelled_waiter_does_not_unlock(monkeypatch):
    fake = _FakeDocker()
    fake.create_delay = 0.05
    manager = _manager(fake, max_workrooms=1)
    monkeypatch.setattr(manager, "_verify_adoption", _fixture_adoption)
    first = asyncio.create_task(manager.ensure_workroom("parallel.same"))
    await asyncio.sleep(0)
    cancelled = asyncio.create_task(manager.ensure_workroom("parallel.same"))
    other = asyncio.create_task(manager.ensure_workroom("parallel.same"))
    cancelled.cancel()
    results = await asyncio.gather(first, cancelled, other, return_exceptions=True)
    assert isinstance(results[1], asyncio.CancelledError)
    assert results[0] == results[2]
    assert len(fake.containers) == 1
    assert sum(args[0] == "create" for args in fake.calls) == 1
    # Completed holders and cancelled waiters do not leave per-key lock entries.
    assert len(manager._ensure_locks) == 0
