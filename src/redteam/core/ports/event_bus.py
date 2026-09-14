"""Event Bus port. Concrete backends in ``redteam.event_bus``.

Initial backend is in-memory (``InMemoryEventBus``); Redis/NATS/Kafka
backends can be added later without changing this Protocol or any
publisher/subscriber code.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Protocol

from redteam.core.contracts.events import EventEnvelope

EventHandler = Callable[[EventEnvelope], Awaitable[None]]


class EventBus(Protocol):
    async def publish(self, event: EventEnvelope) -> None: ...

    async def subscribe(self, event_type: str, handler: EventHandler) -> None: ...
