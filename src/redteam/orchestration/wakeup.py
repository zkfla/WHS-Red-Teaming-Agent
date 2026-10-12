"""Coalesced event wake-ups with periodic reconciliation fallback."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from pydantic import BaseModel, Field

from redteam.core.contracts.events import EventEnvelope, EventType
from redteam.core.ids import EngagementId, EventId, RunId
from redteam.core.ports.event_bus import EventBus

DEFAULT_WAKE_EVENTS = frozenset(
    {
        EventType.GRAPH_UPDATED,
        EventType.EVIDENCE_CREATED,
        EventType.ACTION_APPROVED,
        EventType.ACTION_DENIED,
        EventType.APPROVAL_REQUIRED,
    }
)


class WakeupBatch(BaseModel):
    triggered: bool
    reason: str
    event_count: int = 0
    event_types: list[str] = Field(default_factory=list)
    last_event_id: EventId | None = None
    observed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class EventWakeup:
    """Collapses event bursts into one scheduler tick.

    A timeout intentionally produces a reconciliation tick, so a lossy Event
    Bus cannot permanently hide canonical graph/evidence changes.
    """

    def __init__(
        self,
        *,
        run_id: RunId | None = None,
        engagement_id: EngagementId | None = None,
        event_types: set[str] | frozenset[str] = DEFAULT_WAKE_EVENTS,
    ) -> None:
        self._run_id = run_id
        self._engagement_id = engagement_id
        self._event_types = frozenset(event_types)
        self._signal = asyncio.Event()
        self._lock = asyncio.Lock()
        self._events: dict[EventId, EventEnvelope] = {}
        self._attached = False

    async def attach(self, bus: EventBus) -> None:
        if self._attached:
            return
        for event_type in sorted(self._event_types):
            await bus.subscribe(event_type, self.notify)
        self._attached = True

    @property
    def pending(self) -> bool:
        return self._signal.is_set()

    async def notify(self, event: EventEnvelope) -> None:
        if self._run_id is not None and event.run_id != self._run_id:
            return
        if self._engagement_id is not None and event.engagement_id != self._engagement_id:
            return
        async with self._lock:
            self._events[event.event_id] = event
            self._signal.set()

    async def wait(self, *, timeout_seconds: float) -> WakeupBatch:
        if timeout_seconds <= 0:
            raise ValueError("wakeup timeout must be positive")
        try:
            await asyncio.wait_for(self._signal.wait(), timeout=timeout_seconds)
        except asyncio.TimeoutError:
            return WakeupBatch(triggered=False, reason="periodic_reconciliation")

        async with self._lock:
            events = sorted(
                self._events.values(),
                key=lambda item: (item.timestamp, str(item.event_id)),
            )
            self._events.clear()
            self._signal.clear()
        return WakeupBatch(
            triggered=True,
            reason="event_delta",
            event_count=len(events),
            event_types=sorted({item.event_type for item in events}),
            last_event_id=events[-1].event_id if events else None,
        )
