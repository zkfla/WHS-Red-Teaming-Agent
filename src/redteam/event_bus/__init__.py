"""Event Bus backends. Owned by Agent Team.

Implements ``redteam.core.ports.event_bus.EventBus``. Only this
subpackage may know about the concrete backend (in-memory today; Redis /
NATS / Kafka can be added here later without touching callers, which all
depend on the ``EventBus`` Protocol).
"""

from redteam.event_bus.memory import InMemoryEventBus

__all__ = ["InMemoryEventBus"]
