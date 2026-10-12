import asyncio
from datetime import datetime, timedelta, timezone

from redteam.core.contracts.events import EventEnvelope, EventType
from redteam.core.ids import EngagementId, EventId, RunId
from redteam.event_bus.memory import InMemoryEventBus
from redteam.orchestration.wakeup import EventWakeup, expiry_timeout


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


def test_expiry_timeout_without_deadline_keeps_maximum() -> None:
    assert expiry_timeout(None, max_timeout_seconds=30.0) == 30.0


def test_expiry_timeout_with_past_deadline_returns_zero() -> None:
    now = datetime.now(timezone.utc)
    assert expiry_timeout(now - timedelta(seconds=5), now=now,
                          max_timeout_seconds=30.0) == 0.0


def test_expiry_timeout_clamps_future_deadline_to_maximum() -> None:
    now = datetime.now(timezone.utc)
    assert expiry_timeout(now + timedelta(hours=1), now=now,
                          max_timeout_seconds=30.0) == 30.0
    assert expiry_timeout(now + timedelta(seconds=5), now=now,
                          max_timeout_seconds=30.0) == 5.0


def test_expiry_timeout_rejects_naive_deadline() -> None:
    try:
        expiry_timeout(datetime.now(), max_timeout_seconds=1.0)
    except ValueError:
        return
    raise AssertionError("naive wake_at accepted")


def test_expiry_timeout_rejects_nonpositive_maximum() -> None:
    try:
        expiry_timeout(None, max_timeout_seconds=0.0)
    except ValueError:
        return
    raise AssertionError("non-positive timeout accepted")


async def test_wait_past_deadline_reports_validity_recheck() -> None:
    wakeup = EventWakeup()
    moment = datetime.now(timezone.utc)
    batch = await wakeup.wait(timeout_seconds=5.0, wake_at=moment - timedelta(seconds=1),
                              now=moment)

    assert batch.triggered is False
    assert batch.reason == "validity_recheck"


async def test_wait_future_deadline_keeps_reconciliation_reason() -> None:
    wakeup = EventWakeup()
    moment = datetime.now(timezone.utc)
    batch = await wakeup.wait(timeout_seconds=0.01,
                              wake_at=moment + timedelta(hours=1), now=moment)

    assert batch.triggered is False
    assert batch.reason == "periodic_reconciliation"


async def test_wait_deadline_wakes_early_for_recheck() -> None:
    wakeup = EventWakeup()
    started = asyncio.get_running_loop().time()
    batch = await wakeup.wait(timeout_seconds=30.0,
                              wake_at=datetime.now(timezone.utc) + timedelta(seconds=0.03))

    assert batch.triggered is False
    assert batch.reason == "validity_recheck"
    assert asyncio.get_running_loop().time() - started < 30.0


async def test_wait_events_win_over_passed_deadline() -> None:
    bus = InMemoryEventBus()
    wakeup = EventWakeup(run_id=RunId("run"))
    await wakeup.attach(bus)
    await bus.publish(event("evt_1", EventType.GRAPH_UPDATED))
    batch = await wakeup.wait(timeout_seconds=5.0,
                              wake_at=datetime.now(timezone.utc) - timedelta(seconds=1))

    assert batch.triggered is True
    assert batch.reason == "event_delta"
    assert batch.event_count == 1
