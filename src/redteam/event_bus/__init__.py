"""Event Bus backends. Owned by Agent Team.

Implements ``redteam.core.ports.event_bus.EventBus``. Only this
subpackage may know about the concrete backend (in-memory today; Redis /
NATS / Kafka can be added here later without touching callers, which all
depend on the ``EventBus`` Protocol).

``EventPublisher`` is the producer-side helper: it binds a run and a
source so every envelope carries consistent correlation/causation ids
instead of each call site assembling them by hand.
"""

from redteam.event_bus.memory import WILDCARD, InMemoryEventBus
from redteam.event_bus.publisher import EventPublisher

__all__ = ["InMemoryEventBus", "EventPublisher", "WILDCARD"]
