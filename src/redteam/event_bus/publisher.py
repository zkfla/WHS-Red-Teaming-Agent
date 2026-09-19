"""``EventPublisher`` — builds ``EventEnvelope``s for one run + source.

Fills in run/engagement/source/timestamp and carries correlation and
causation ids. Rationale: ``docs/agent/event-bus.md``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from redteam.core.contracts.events import EventEnvelope
from redteam.core.ids import EngagementId, EventId, RunId, new_id
from redteam.core.ports.event_bus import EventBus


class EventPublisher:
    """Publishes envelopes for one run, from one source."""

    __slots__ = ("_bus", "_run_id", "_engagement_id", "_source", "_default_correlation_id")

    def __init__(
        self,
        bus: EventBus,
        *,
        run_id: RunId,
        engagement_id: EngagementId,
        source: str,
        correlation_id: str | None = None,
    ) -> None:
        self._bus = bus
        self._run_id = run_id
        self._engagement_id = engagement_id
        self._source = source
        self._default_correlation_id = correlation_id or str(run_id)

    @property
    def source(self) -> str:
        return self._source

    def for_source(self, source: str) -> EventPublisher:
        """Same run, different source — one per orchestration node."""
        return EventPublisher(
            self._bus,
            run_id=self._run_id,
            engagement_id=self._engagement_id,
            source=source,
            correlation_id=self._default_correlation_id,
        )

    def build(
        self,
        event_type: str,
        payload: dict[str, Any] | None = None,
        *,
        correlation_id: str | None = None,
        causation_id: str | None = None,
    ) -> EventEnvelope:
        return EventEnvelope(
            event_id=EventId(new_id("evt")),
            event_type=event_type,
            run_id=self._run_id,
            engagement_id=self._engagement_id,
            source=self._source,
            timestamp=datetime.now(timezone.utc),
            correlation_id=correlation_id or self._default_correlation_id,
            causation_id=causation_id,
            payload=dict(payload or {}),
        )

    async def publish(
        self,
        event_type: str,
        payload: dict[str, Any] | None = None,
        *,
        correlation_id: str | None = None,
        causation_id: str | None = None,
    ) -> EventEnvelope:
        """Publish and return the envelope, so the next event can cite it."""
        event = self.build(
            event_type,
            payload,
            correlation_id=correlation_id,
            causation_id=causation_id,
        )
        await self._bus.publish(event)
        return event
