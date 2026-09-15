"""``RuntimeDeps`` — what every orchestration node is handed.

Other teams are reached only through ``core.ports`` Protocols, wired in
``redteam.app.bootstrap``. ``agents``/``settings`` are Agent-Team-internal
and default, so existing call sites keep working.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from redteam.core.ports.attack_graph import AttackGraphStore
from redteam.core.ports.control_plane import ControlPlane
from redteam.core.ports.event_bus import EventBus
from redteam.core.ports.evidence_store import EvidenceStore
from redteam.core.ports.llm import LLMClient
from redteam.core.ports.tool_gateway import ToolGateway
from redteam.orchestration.agents.registry import AgentRegistry, default_registry
from redteam.orchestration.settings import OrchestrationSettings


@dataclass(frozen=True)
class RuntimeDeps:
    control_plane: ControlPlane
    tool_gateway: ToolGateway
    event_bus: EventBus
    graph_store: AttackGraphStore
    evidence_store: EvidenceStore
    llm_client: LLMClient

    agents: AgentRegistry = field(default_factory=default_registry)
    settings: OrchestrationSettings = field(default_factory=OrchestrationSettings)
