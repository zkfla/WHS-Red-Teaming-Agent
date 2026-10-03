"""``build_test_application`` — an all-Fake ``RuntimeDeps`` (instruction.md
section 21's ``ApplicationServices``) for teams to write tests against
without a real Engagement, Control Plane, or MCP server.

Compare ``redteam.app.bootstrap.build_runtime_deps``, which wires the
real/default backends (used by the CLI and integration tests).
"""

from __future__ import annotations

from redteam.attack_graph.stores.memory import InMemoryAttackGraphStore
from redteam.core.contracts.tool import ToolDescriptor
from redteam.core.ids import ToolId
from redteam.core.ports.attack_graph import AttackGraphStore
from redteam.core.ports.control_plane import ControlPlane
from redteam.core.ports.event_bus import EventBus
from redteam.core.ports.evidence_store import EvidenceStore
from redteam.core.ports.llm import LLMClient
from redteam.core.ports.tool_catalog import ToolCatalog
from redteam.core.ports.tool_gateway import ToolGateway
from redteam.event_bus.memory import InMemoryEventBus
from redteam.llm.fake import FakeLLMClient
from redteam.orchestration.agents.providers import (
    CandidateProviderRegistry,
    default_provider_registry,
)
from redteam.orchestration.agents.registry import AgentRegistry, default_registry
from redteam.orchestration.analysis import AnalysisVerifier, DeterministicAnalysisVerifier
from redteam.orchestration.context import RuntimeDeps
from redteam.orchestration.settings import OrchestrationSettings
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
    verifier: AnalysisVerifier | None = None,
    agents: AgentRegistry | None = None,
    candidate_providers: CandidateProviderRegistry | None = None,
    tool_catalog: ToolCatalog | None = None,
    settings: OrchestrationSettings | None = None,
) -> RuntimeDeps:
    return RuntimeDeps(
        control_plane=control_plane or FakeControlPlane(),
        tool_gateway=tool_gateway or DummyToolGateway(),
        event_bus=event_bus or InMemoryEventBus(),
        graph_store=graph_store or InMemoryAttackGraphStore(),
        evidence_store=evidence_store or FakeEvidenceStore(),
        llm_client=llm_client or FakeLLMClient(),
        verifier=verifier or DeterministicAnalysisVerifier(),
        # `is not None`, not `or`: an intentionally empty AgentRegistry
        # is falsy and must not be replaced by the default one.
        agents=agents if agents is not None else default_registry(),
        candidate_providers=(
            candidate_providers if candidate_providers is not None else default_provider_registry()
        ),
        # Node-level tests manually supply compiled actions. Their default
        # assembly must still include explicit intent bindings, as production
        # bootstrap does; an unconfigured Catalog is no longer admissible.
        tool_catalog=tool_catalog
        if tool_catalog is not None
        else DummyToolGateway(
            tools=[
                ToolDescriptor(
                    tool_id=ToolId("recon.probe"),
                    server_id="test",
                    tool_name="probe",
                    capability_tags=["recon"],
                    operations=["service_discovery", "auth_attempt"],
                ),
                ToolDescriptor(
                    tool_id=ToolId("recon.http_probe"),
                    server_id="test",
                    tool_name="http_probe",
                    capability_tags=["recon"],
                    operations=["service_discovery", "endpoint_discovery"],
                ),
            ]
        ),
        settings=settings if settings is not None else OrchestrationSettings(),
    )
