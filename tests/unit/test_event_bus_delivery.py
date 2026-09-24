"""InMemoryEventBus delivery guarantees callers rely on."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import NoReturn

from redteam.core.contracts.events import EventEnvelope
from redteam.core.ids import EngagementId, EventId, RunId
from redteam.event_bus.memory import InMemoryEventBus


def event(event_type: str, correlation_id: str | None = None) -> EventEnvelope:
    return EventEnvelope(
        event_id=EventId("evt_1"),
        event_type=event_type,
        run_id=RunId("run_1"),
        engagement_id=EngagementId("eng_1"),
        source="test",
        timestamp=datetime.now(timezone.utc),
        correlation_id=correlation_id,
    )


async def test_a_failing_handler_cannot_break_the_others_or_the_publisher() -> None:
    # A broken subscriber must not be able to abort a run.
    bus = InMemoryEventBus()
    delivered: list[str] = []

    async def boom(_event: EventEnvelope) -> None:
        await asyncio.sleep(0)
        raise RuntimeError("subscriber exploded")

    async def collect(evt: EventEnvelope) -> None:
        delivered.append(evt.event_type)

    await bus.subscribe("*", boom)
    await bus.subscribe("*", collect)
    await bus.publish(event("RunStarted"))

    assert delivered == ["RunStarted"]
    assert bus.handler_failures == 1


async def test_synchronous_handler_failure_is_isolated() -> None:
    bus = InMemoryEventBus()
    delivered: list[str] = []

    def boom(_event: EventEnvelope) -> NoReturn:
        raise RuntimeError("subscriber call exploded")

    async def collect(evt: EventEnvelope) -> None:
        delivered.append(evt.event_type)

    await bus.subscribe("RunStarted", boom)
    await bus.subscribe("RunStarted", collect)
    await bus.publish(event("RunStarted"))

    assert delivered == ["RunStarted"]
    assert bus.handler_failures == 1


async def test_synchronous_failures_preserve_duplicate_and_wildcard_delivery() -> None:
    bus = InMemoryEventBus()
    failed: list[str] = []
    delivered: list[str] = []

    def boom(evt: EventEnvelope) -> NoReturn:
        failed.append(evt.event_type)
        raise RuntimeError("subscriber call exploded")

    async def collect(evt: EventEnvelope) -> None:
        delivered.append(evt.event_type)

    await bus.subscribe("RunStarted", boom)
    await bus.subscribe("RunStarted", boom)
    await bus.subscribe("*", boom)
    await bus.subscribe("RunStarted", collect)
    await bus.subscribe("*", collect)
    await bus.publish(event("RunStarted"))

    assert failed == ["RunStarted"] * 3
    assert delivered == ["RunStarted"] * 2
    assert bus.handler_failures == 3


async def test_synchronous_failure_still_waits_for_concurrent_handlers() -> None:
    bus = InMemoryEventBus()
    started = asyncio.Event()
    release = asyncio.Event()
    completed: list[str] = []
    published = event("RunStarted")

    def boom(_event: EventEnvelope) -> NoReturn:
        raise RuntimeError("subscriber call exploded")

    async def slow(_event: EventEnvelope) -> None:
        assert bus.history() == [published]
        started.set()
        await release.wait()
        completed.append("slow")

    async def peer(_event: EventEnvelope) -> None:
        await started.wait()
        completed.append("peer")

    await bus.subscribe("RunStarted", boom)
    await bus.subscribe("RunStarted", slow)
    await bus.subscribe("RunStarted", peer)
    publishing = asyncio.create_task(bus.publish(published))
    try:
        await asyncio.wait_for(started.wait(), timeout=1)
        await asyncio.sleep(0)
        assert not publishing.done()
        assert completed == ["peer"]
    finally:
        release.set()
        await asyncio.wait_for(publishing, timeout=1)

    assert completed == ["peer", "slow"]
    assert bus.handler_failures == 1


async def test_handlers_start_concurrently_and_publish_waits_for_all() -> None:
    bus = InMemoryEventBus()
    started = asyncio.Event()
    release = asyncio.Event()
    started_count = 0
    completed: list[str] = []
    published = event("RunStarted")

    async def slow(name: str, _event: EventEnvelope) -> None:
        nonlocal started_count
        started_count += 1
        if started_count == 2:
            started.set()
        await release.wait()
        completed.append(name)

    await bus.subscribe("RunStarted", lambda evt: slow("first", evt))
    await bus.subscribe("RunStarted", lambda evt: slow("second", evt))

    publishing = asyncio.create_task(bus.publish(published))
    try:
        await asyncio.wait_for(started.wait(), timeout=1)
        assert bus.history() == [published]
        assert not publishing.done()
        assert completed == []
    finally:
        release.set()
        await asyncio.wait_for(publishing, timeout=1)

    assert sorted(completed) == ["first", "second"]


async def test_cancelled_handler_is_isolated_and_other_handlers_complete() -> None:
    bus = InMemoryEventBus()
    completed: list[str] = []

    async def cancelled(_event: EventEnvelope) -> None:
        raise asyncio.CancelledError()

    async def collect(_event: EventEnvelope) -> None:
        completed.append("collect")

    await bus.subscribe("RunStarted", cancelled)
    await bus.subscribe("RunStarted", collect)
    await bus.publish(event("RunStarted"))

    assert completed == ["collect"]
    assert bus.handler_failures == 1


async def test_subscription_count_preserves_duplicate_and_wildcard_registrations() -> None:
    bus = InMemoryEventBus()

    async def handler(_event: EventEnvelope) -> None:
        return None

    await bus.subscribe("RunStarted", handler)
    await bus.subscribe("RunStarted", handler)
    await bus.subscribe("*", handler)

    assert bus.subscription_count("RunStarted") == 2
    assert bus.subscription_count("*") == 1
    assert bus.subscription_count() == 3


async def test_history_preserves_publication_order() -> None:
    bus = InMemoryEventBus()
    for name in ("RunStarted", "ActionProposed", "RunFinished"):
        await bus.publish(event(name))
    assert bus.event_types() == ["RunStarted", "ActionProposed", "RunFinished"]


async def test_history_is_bounded() -> None:
    # A ring buffer for debugging — the Evidence Store is what keeps the
    # record that matters.
    bus = InMemoryEventBus(history_limit=2)
    for name in ("a", "b", "c"):
        await bus.publish(event(name))
    assert bus.event_types() == ["b", "c"]


async def test_history_can_be_filtered_and_cleared() -> None:
    bus = InMemoryEventBus()
    await bus.publish(event("RunStarted", correlation_id="action_1"))
    await bus.publish(event("RunFinished"))

    assert len(bus.history(event_type="RunStarted")) == 1
    assert len(bus.correlated("action_1")) == 1

    bus.clear_history()
    assert bus.history() == []


async def test_events_are_recorded_even_with_no_subscribers() -> None:
    bus = InMemoryEventBus()
    await bus.publish(event("RunStarted"))
    assert len(bus.history()) == 1
