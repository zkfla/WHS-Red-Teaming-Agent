from datetime import datetime, timezone

from redteam.core.contracts.events import EventEnvelope, EventType
from redteam.core.ids import EngagementId, EventId, RunId
from redteam.event_bus.memory import InMemoryEventBus
from redteam.orchestration.wakeup import EventWakeup


def event(event_id: str, event_type: str, *, run_id: str = "run") -> EventEnvelope:
    return EventEnvelope(
        event_id=EventId(event_id),
        event_type=event_type,
        run_id=RunId(run_id),
        engagement_id=EngagementId("eng"),
        source="test",
        timestamp=datetime.now(timezone.utc),
    )


async def test_event_burst_is_coalesced_and_deduplicated() -> None:
    bus = InMemoryEventBus()
    wakeup = EventWakeup(run_id=RunId("run"))
    await wakeup.attach(bus)
    first = event("evt_1", EventType.GRAPH_UPDATED)

    await bus.publish(first)
    await bus.publish(first)
    await bus.publish(event("evt_2", EventType.EVIDENCE_CREATED))
    await bus.publish(event("evt_other", EventType.GRAPH_UPDATED, run_id="other"))
    batch = await wakeup.wait(timeout_seconds=0.1)

    assert batch.triggered is True
    assert batch.event_count == 2
    assert batch.event_types == [EventType.EVIDENCE_CREATED, EventType.GRAPH_UPDATED]


async def test_timeout_requests_periodic_reconciliation() -> None:
    wakeup = EventWakeup()
    batch = await wakeup.wait(timeout_seconds=0.01)

    assert batch.triggered is False
    assert batch.reason == "periodic_reconciliation"
