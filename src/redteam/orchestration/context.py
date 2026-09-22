"""``RuntimeDeps`` — what every orchestration node is handed.

Other teams are reached only through ``core.ports`` Protocols, wired in
``redteam.app.bootstrap``. ``agents``/``settings`` are Agent-Team-internal
and default, so existing call sites keep working.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from redteam.core.ports.attack_graph import AttackGraphStore
from redteam.core.ports.control_plane import ControlPlane
from redteam.core.ports.event_bus import EventBus
from redteam.core.ports.evidence_store import EvidenceStore
from redteam.core.ports.llm import LLMClient
from redteam.core.ports.tool_catalog import ToolCatalog
from redteam.core.ports.tool_gateway import ToolGateway
from redteam.orchestration.agents.providers import (
    CandidateProviderRegistry,
    default_provider_registry,
)
from redteam.orchestration.agents.registry import AgentRegistry, default_registry
from redteam.orchestration.analysis import AnalysisVerifier, DeterministicAnalysisVerifier
from redteam.orchestration.context_projection import ContextProjectionCache
from redteam.orchestration.observations import ResultMapperRegistry, default_result_mapper_registry
from redteam.orchestration.settings import OrchestrationSettings
from redteam.orchestration.work.memory import InMemoryWorkStore
from redteam.orchestration.work.store import WorkStore


@dataclass(frozen=True)
class RuntimeDeps:
    control_plane: ControlPlane
    tool_gateway: ToolGateway
    event_bus: EventBus
    graph_store: AttackGraphStore
    evidence_store: EvidenceStore
    llm_client: LLMClient
    verifier: AnalysisVerifier = field(default_factory=DeterministicAnalysisVerifier)

    agents: AgentRegistry = field(default_factory=default_registry)
    candidate_providers: CandidateProviderRegistry = field(
        default_factory=default_provider_registry
    )
    # Agent-Team view of the Capability registry.  ``None`` keeps legacy
    # playbooks operational until Capability/PM finalize a shared catalog port.
    tool_catalog: ToolCatalog | None = None
    # Tool Result -> Attack Graph Mutation (SERVICE/ENDPOINT discovery from
    # a successful execution's real output) -- see `orchestration.observations`.
    result_mappers: ResultMapperRegistry = field(default_factory=default_result_mapper_registry)
    work_store: WorkStore = field(default_factory=InMemoryWorkStore)
    settings: OrchestrationSettings = field(default_factory=OrchestrationSettings)
    context_cache: ContextProjectionCache = field(
        default_factory=ContextProjectionCache, compare=False
    )
    # DefaultControlPlane owns mutable rate/budget counters. Policy admission
    # is intentionally serialized while approved tool calls may run in parallel.
    policy_lock: asyncio.Lock = field(default_factory=asyncio.Lock, compare=False)
