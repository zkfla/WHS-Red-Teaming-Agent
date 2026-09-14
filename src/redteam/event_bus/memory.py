"""In-memory ``EventBus`` implementation.

Good enough for the skeleton and for tests. Handlers subscribed to
``"*"`` receive every event regardless of ``event_type`` — useful for a
catch-all audit/logging subscriber (see ``app.cli``).
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict

from redteam.core.contracts.events import EventEnvelope
from redteam.core.ports.event_bus import EventHandler

logger = logging.getLogger(__name__)


class InMemoryEventBus:
    def __init__(self) -> None:
        self._handlers: dict[str, list[EventHandler]] = defaultdict(list)

    async def publish(self, event: EventEnvelope) -> None:
        handlers = [*self._handlers.get(event.event_type, []), *self._handlers.get("*", [])]
        if not handlers:
            return
        results = await asyncio.gather(
            *(handler(event) for handler in handlers), return_exceptions=True
        )
        for result in results:
            if isinstance(result, Exception):
                logger.exception("event handler failed for %s", event.event_type, exc_info=result)

    async def subscribe(self, event_type: str, handler: EventHandler) -> None:
        self._handlers[event_type].append(handler)
