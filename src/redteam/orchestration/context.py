"""External ports handed to every orchestration node.

Nodes depend only on ``core`` Protocols here, never on concrete
Control/Evidence/Capability implementations — those are wired once in
``redteam.app.bootstrap`` and passed down as a ``RuntimeDeps``.
"""

from __future__ import annotations

from dataclasses import dataclass

from redteam.core.ports.attack_graph import AttackGraphStore
from redteam.core.ports.control_plane import ControlPlane
from redteam.core.ports.event_bus import EventBus
from redteam.core.ports.evidence_store import EvidenceStore
from redteam.core.ports.llm import LLMClient
from redteam.core.ports.tool_gateway import ToolGateway


@dataclass(frozen=True)
class RuntimeDeps:
    control_plane: ControlPlane
    tool_gateway: ToolGateway
    event_bus: EventBus
    graph_store: AttackGraphStore
    evidence_store: EvidenceStore
    llm_client: LLMClient
