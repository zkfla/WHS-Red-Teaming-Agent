"""``build_test_application`` — an all-Fake ``RuntimeDeps`` (instruction.md
section 21's ``ApplicationServices``) for teams to write tests against
without a real Engagement, Control Plane, or MCP server.

Compare ``redteam.app.bootstrap.build_runtime_deps``, which wires the
real/default backends (used by the CLI and integration tests).
"""

from __future__ import annotations

from redteam.attack_graph.stores.memory import InMemoryAttackGraphStore
from redteam.core.ports.attack_graph import AttackGraphStore
from redteam.core.ports.control_plane import ControlPlane
from redteam.core.ports.event_bus import EventBus
from redteam.core.ports.evidence_store import EvidenceStore
from redteam.core.ports.llm import LLMClient
from redteam.core.ports.tool_gateway import ToolGateway
from redteam.event_bus.memory import InMemoryEventBus
from redteam.llm.fake import FakeLLMClient
from redteam.orchestration.context import RuntimeDeps
from tests.fakes.control_plane import FakeControlPlane
from tests.fakes.evidence_store import FakeEvidenceStore
from tests.fakes.tool_gateway import DummyToolGateway


def build_test_application(
    *,
    control_plane: ControlPlane | None = None,
    tool_gateway: ToolGateway | None = None,
    event_bus: EventBus | None = None,
    graph_store: AttackGraphStore | None = None,
    evidence_store: EvidenceStore | None = None,
    llm_client: LLMClient | None = None,
) -> RuntimeDeps:
    return RuntimeDeps(
        control_plane=control_plane or FakeControlPlane(),
        tool_gateway=tool_gateway or DummyToolGateway(),
        event_bus=event_bus or InMemoryEventBus(),
        graph_store=graph_store or InMemoryAttackGraphStore(),
        evidence_store=evidence_store or FakeEvidenceStore(),
        llm_client=llm_client or FakeLLMClient(),
    )
