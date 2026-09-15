"""InMemoryEventBus delivery guarantees callers rely on."""

from __future__ import annotations

from datetime import datetime, timezone

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
        raise RuntimeError("subscriber exploded")

    async def collect(evt: EventEnvelope) -> None:
        delivered.append(evt.event_type)

    await bus.subscribe("*", boom)
    await bus.subscribe("*", collect)
    await bus.publish(event("RunStarted"))

    assert delivered == ["RunStarted"]
    assert bus.handler_failures == 1


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
