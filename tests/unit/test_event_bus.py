"""Event Test: publish delivers to matching and wildcard subscribers."""

from __future__ import annotations

from datetime import datetime, timezone

from redteam.core.contracts.events import EventEnvelope
from redteam.core.ids import EngagementId, EventId, RunId
from redteam.event_bus.memory import InMemoryEventBus


def _event(event_type: str) -> EventEnvelope:
    return EventEnvelope(
        event_id=EventId("evt_1"),
        event_type=event_type,
        run_id=RunId("run_1"),
        engagement_id=EngagementId("eng_1"),
        source="test",
        timestamp=datetime.now(timezone.utc),
    )


async def test_subscriber_receives_matching_event_type() -> None:
    bus = InMemoryEventBus()
    received: list[EventEnvelope] = []

    async def handler(event: EventEnvelope) -> None:
        received.append(event)

    await bus.subscribe("RunStarted", handler)
    await bus.publish(_event("RunStarted"))
    await bus.publish(_event("RunFinished"))

    assert [e.event_type for e in received] == ["RunStarted"]


async def test_wildcard_subscriber_receives_every_event() -> None:
    bus = InMemoryEventBus()
    received: list[str] = []

    async def handler(event: EventEnvelope) -> None:
        received.append(event.event_type)

    await bus.subscribe("*", handler)
    await bus.publish(_event("RunStarted"))
    await bus.publish(_event("ActionProposed"))

    assert received == ["RunStarted", "ActionProposed"]
