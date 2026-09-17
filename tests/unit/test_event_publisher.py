"""EventPublisher: correlation and causation survive every hop."""

from __future__ import annotations

from datetime import timezone

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


async def test_custom_default_correlation_override_does_not_mutate_default() -> None:
    bus = InMemoryEventBus()
    pub = EventPublisher(
        bus,
        run_id=RUN,
        engagement_id=ENGAGEMENT,
        source="test",
        correlation_id="workflow-1",
    )

    overridden = await pub.publish(EventType.ACTION_PROPOSED, correlation_id="action-1")
    restored = await pub.publish(EventType.RUN_FINISHED)

    assert overridden.correlation_id == "action-1"
    assert restored.correlation_id == "workflow-1"


async def test_for_source_preserves_bus_run_engagement_and_default_correlation() -> None:
    bus = InMemoryEventBus()
    original = EventPublisher(
        bus,
        run_id=RUN,
        engagement_id=ENGAGEMENT,
        source="original",
        correlation_id="workflow-1",
    )

    derived = original.for_source("derived")
    event = await derived.publish(EventType.EVIDENCE_CREATED)

    assert derived.source == "derived"
    assert original.source == "original"
    assert event.source == "derived"
    assert event.run_id == RUN
    assert event.engagement_id == ENGAGEMENT
    assert event.correlation_id == "workflow-1"
    assert bus.history() == [event]


async def test_each_published_event_gets_a_new_id_and_utc_timestamp() -> None:
    bus = InMemoryEventBus()
    pub = publisher(bus)

    first = await pub.publish(EventType.ACTION_PROPOSED)
    second = await pub.publish(EventType.ACTION_PROPOSED)

    assert first.event_id != second.event_id
    assert first.timestamp.tzinfo == timezone.utc
    assert second.timestamp.tzinfo == timezone.utc


def test_payload_is_copied_at_build_time() -> None:
    payload = {"capability": "recon", "operation": "port_scan"}
    event = publisher(InMemoryEventBus()).build(EventType.ACTION_PROPOSED, payload)

    payload["operation"] = "changed_after_build"

    assert event.payload == {"capability": "recon", "operation": "port_scan"}


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


async def test_fan_out_children_keep_correlation_and_share_parent_causation() -> None:
    bus = InMemoryEventBus()
    pub = publisher(bus)
    parent = await pub.publish(EventType.ACTION_PROPOSED, correlation_id="action-1")

    first = await pub.publish(
        EventType.POLICY_EVALUATED,
        correlation_id="action-1",
        causation_id=parent.event_id,
    )
    second = await pub.publish(
        EventType.ACTION_APPROVED,
        correlation_id="action-1",
        causation_id=parent.event_id,
    )

    assert first.correlation_id == second.correlation_id == parent.correlation_id
    assert first.causation_id == second.causation_id == parent.event_id


async def test_publishers_for_different_runs_do_not_share_default_correlation() -> None:
    bus = InMemoryEventBus()
    first = await publisher(bus).publish(EventType.RUN_STARTED)
    second = await EventPublisher(
        bus,
        run_id=RunId("run_2"),
        engagement_id=ENGAGEMENT,
        source="test",
    ).publish(EventType.RUN_STARTED)

    assert first.correlation_id != second.correlation_id


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
