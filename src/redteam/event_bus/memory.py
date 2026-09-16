"""In-memory ``EventBus``.

Ordered delivery, handler-failure isolation, and a bounded ring buffer of
recent events for debugging (not durable storage — that is the Evidence
Store). ``"*"`` subscribes to every event type.
Rationale: ``docs/agent/event-bus.md``.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict, deque

from redteam.core.contracts.events import EventEnvelope
from redteam.core.ports.event_bus import EventHandler

logger = logging.getLogger(__name__)

WILDCARD = "*"
DEFAULT_HISTORY_LIMIT = 1000


class InMemoryEventBus:
    def __init__(self, *, history_limit: int = DEFAULT_HISTORY_LIMIT) -> None:
        self._handlers: dict[str, list[EventHandler]] = defaultdict(list)
        self._history: deque[EventEnvelope] = deque(maxlen=history_limit)
        self._handler_failures = 0

    async def publish(self, event: EventEnvelope) -> None:
        self._history.append(event)
        handlers = [*self._handlers.get(event.event_type, []), *self._handlers.get(WILDCARD, [])]
        if not handlers:
            return

        async def invoke(handler: EventHandler) -> None:
            await handler(event)

        results = await asyncio.gather(
            *(invoke(handler) for handler in handlers), return_exceptions=True
        )
        for result in results:
            if isinstance(result, BaseException):
                self._handler_failures += 1
                logger.exception("event handler failed for %s", event.event_type, exc_info=result)

    async def subscribe(self, event_type: str, handler: EventHandler) -> None:
        self._handlers[event_type].append(handler)

    # -- introspection (in-memory backend only; not part of the port) --

    def history(self, *, event_type: str | None = None) -> list[EventEnvelope]:
        """Recently published events, oldest first. Debugging/tests only."""
        if event_type is None:
            return list(self._history)
        return [event for event in self._history if event.event_type == event_type]

    def event_types(self) -> list[str]:
        return [event.event_type for event in self._history]

    def correlated(self, correlation_id: str) -> list[EventEnvelope]:
        """Every recent event about one action/run — the audit trail view."""
        return [event for event in self._history if event.correlation_id == correlation_id]

    @property
    def handler_failures(self) -> int:
        return self._handler_failures

    def clear_history(self) -> None:
        self._history.clear()
