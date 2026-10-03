"""Event Bus port. Concrete backends in ``redteam.event_bus``.

Initial backend is in-memory (``InMemoryEventBus``); Redis/NATS/Kafka
backends can be added later without changing this Protocol or any
publisher/subscriber code.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Protocol, runtime_checkable

from redteam.core.contracts.events import EventEnvelope

EventHandler = Callable[[EventEnvelope], Awaitable[None]]


class EventBus(Protocol):
    async def publish(self, event: EventEnvelope) -> None: ...

    async def subscribe(self, event_type: str, handler: EventHandler) -> None: ...


@runtime_checkable
class EventHistory(Protocol):
    """Explicit read contract for event introspection (M29).

    Separate from the live publish/subscribe port above: a bus backend
    may transport events without retaining them. Consumers that need
    past events (operator explanations, diagnostics) depend on this
    contract instead of probing for an unofficial history method.
    InMemoryEventBus retains a bounded ring buffer and satisfies
    this contract; backends without retention simply do not provide it
    and callers report history as unavailable.
    """

    def history(self, *, event_type: str | None = None) -> list[EventEnvelope]: ...
