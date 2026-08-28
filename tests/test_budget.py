import asyncio
from pathlib import Path

import pytest

from whs_redteam.exceptions import (
    RateLimitExceeded,
    RequestLimitExceeded,
    RuntimeLimitExceeded,
)
from whs_redteam.inventory import InventoryStore
from whs_redteam.policy.budget import PersistentRequestBudget, RequestBudget


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_request_count_limit() -> None:
    clock = FakeClock()
    budget = RequestBudget(2, 10, 60, clock=clock)
    budget.consume()
    budget.consume()
    with pytest.raises(RequestLimitExceeded):
        budget.consume()


def test_rate_limit() -> None:
    clock = FakeClock()
    budget = RequestBudget(10, 2, 60, clock=clock)
    budget.consume()
    budget.consume()
    with pytest.raises(RateLimitExceeded):
        budget.consume()
    clock.advance(1.01)
    budget.consume()


def test_runtime_limit() -> None:
    clock = FakeClock()
    budget = RequestBudget(10, 10, 5, clock=clock)
    clock.advance(5)
    with pytest.raises(RuntimeLimitExceeded):
        budget.consume()


def test_async_budget_throttles_instead_of_rejecting() -> None:
    clock = FakeClock()
    budget = RequestBudget(10, 2, 60, clock=clock)
    delays: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        delays.append(seconds)
        clock.advance(seconds + 0.001)

    async def scenario() -> None:
        await budget.acquire(sleep=fake_sleep)
        await budget.acquire(sleep=fake_sleep)
        await budget.acquire(sleep=fake_sleep)

    asyncio.run(scenario())
    assert delays
    assert budget.request_count == 3


def test_async_budget_fails_on_total_request_limit() -> None:
    clock = FakeClock()
    budget = RequestBudget(1, 10, 60, clock=clock)

    async def scenario() -> None:
        await budget.acquire()
        with pytest.raises(RequestLimitExceeded):
            await budget.acquire()

    asyncio.run(scenario())


def test_persistent_budget_is_shared_across_instances(tmp_path: Path) -> None:
    clock = FakeClock()
    store = InventoryStore(tmp_path / "inventory.db")
    try:
        first = PersistentRequestBudget(
            "run-shared", 2, 10, 60, store, clock=clock
        )
        first.consume()
        second = PersistentRequestBudget(
            "run-shared", 2, 10, 60, store, clock=clock
        )
        second.consume()
        with pytest.raises(RequestLimitExceeded):
            first.consume()
        first_count = first.request_count
        second_count = second.request_count
    finally:
        store.close()

    assert first_count == 2
    assert second_count == 2


def test_persistent_budget_runtime_survives_reconstruction(tmp_path: Path) -> None:
    clock = FakeClock()
    store = InventoryStore(tmp_path / "inventory.db")
    try:
        PersistentRequestBudget("run-time", 10, 10, 5, store, clock=clock)
        clock.advance(5)
        reconstructed = PersistentRequestBudget(
            "run-time", 10, 10, 5, store, clock=clock
        )
        with pytest.raises(RuntimeLimitExceeded):
            reconstructed.consume()
    finally:
        store.close()


def test_persistent_budget_excludes_paused_approval_time(tmp_path: Path) -> None:
    clock = FakeClock()
    store = InventoryStore(tmp_path / "inventory.db")
    try:
        budget = PersistentRequestBudget(
            "run-paused", 10, 10, 5, store, clock=clock
        )
        clock.advance(1)
        budget.pause()
        clock.advance(100)
        with pytest.raises(RuntimeLimitExceeded, match="paused"):
            budget.consume()
        budget.resume()
        budget.consume()
        clock.advance(3.9)
        budget.consume()
        clock.advance(0.1)
        with pytest.raises(RuntimeLimitExceeded):
            budget.consume()
    finally:
        store.close()
