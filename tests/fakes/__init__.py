"""Test doubles for every Core Port (instruction.md section 20).

**NOT production implementations.** Real backends live under
``redteam.control``, ``redteam.capability``, ``redteam.evidence``,
``redteam.event_bus``, ``redteam.attack_graph``, ``redteam.llm`` — those
are what ship. These exist so any team can write tests against another
team's port without standing up that team's real implementation.

``InMemoryAttackGraphStore`` and ``InMemoryEventBus`` are re-exported
here rather than duplicated: they're already minimal/dependency-free
enough to double as their own fakes (see ``redteam.attack_graph.stores.memory``,
``redteam.event_bus.memory``). ``FakeControlPlane``, ``DummyToolGateway``,
and ``FakeEvidenceStore`` are genuinely new — the real
``DefaultControlPlane``/``MCPToolGateway``/``FilesystemEvidenceStore``
need an ``Engagement``/registered adapters/a filesystem, which a unit
test of unrelated code shouldn't have to set up.
"""

from redteam.attack_graph.stores.memory import InMemoryAttackGraphStore as FakeAttackGraphStore
from redteam.event_bus.memory import InMemoryEventBus as FakeEventBus
from redteam.llm.fake import FakeLLMClient
from tests.fakes.bootstrap import build_test_application
from tests.fakes.control_plane import FakeControlPlane
from tests.fakes.evidence_store import FakeEvidenceStore
from tests.fakes.tool_gateway import DummyToolGateway

__all__ = [
    "FakeControlPlane",
    "DummyToolGateway",
    "FakeEvidenceStore",
    "FakeEventBus",
    "FakeAttackGraphStore",
    "FakeLLMClient",
    "build_test_application",
]
