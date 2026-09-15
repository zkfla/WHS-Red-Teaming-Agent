"""EventPublisher: correlation and causation survive every hop."""

from __future__ import annotations

from redteam.core.contracts.events import EventType
from redteam.core.ids import EngagementId, RunId
from redteam.event_bus.memory import InMemoryEventBus
from redteam.event_bus.publisher import EventPublisher

RUN = RunId("run_1")
ENGAGEMENT = EngagementId("eng_1")


def publisher(bus: InMemoryEventBus, source: str = "orchestration.plan") -> EventPublisher:
    return EventPublisher(bus, run_id=RUN, engagement_id=ENGAGEMENT, source=source)


async def test_run_and_engagement_are_filled_in_for_every_event() -> None:
    bus = InMemoryEventBus()
    event = await publisher(bus).publish(EventType.ACTION_PROPOSED, {"action_id": "a1"})

    assert (event.run_id, event.engagement_id) == (RUN, ENGAGEMENT)
    assert event.source == "orchestration.plan"
    assert event.payload == {"action_id": "a1"}
    assert bus.history() == [event]


async def test_correlation_defaults_to_the_run() -> None:
    event = await publisher(InMemoryEventBus()).publish(EventType.RUN_STARTED)
    assert event.correlation_id == str(RUN)


async def test_a_chain_of_events_stays_walkable() -> None:
    bus = InMemoryEventBus()
    pub = publisher(bus)

    proposed = await pub.publish(EventType.ACTION_PROPOSED, correlation_id="action_1")
    evaluated = await pub.publish(
        EventType.POLICY_EVALUATED, correlation_id="action_1", causation_id=proposed.event_id
    )

    assert evaluated.causation_id == proposed.event_id
    assert [e.event_type for e in bus.correlated("action_1")] == [
        EventType.ACTION_PROPOSED,
        EventType.POLICY_EVALUATED,
    ]


async def test_for_source_keeps_the_run_and_changes_only_the_source() -> None:
    bus = InMemoryEventBus()
    derived = publisher(bus).for_source("orchestration.verify")
    event = await derived.publish(EventType.EVIDENCE_CREATED)

    assert derived.source == "orchestration.verify"
    assert event.run_id == RUN


def test_build_does_not_publish() -> None:
    bus = InMemoryEventBus()
    publisher(bus).build(EventType.RUN_STARTED)
    assert bus.history() == []
