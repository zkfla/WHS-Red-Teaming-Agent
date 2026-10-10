"""``RuntimeDeps`` — what every orchestration node is handed.

Other teams are reached only through ``core.ports`` Protocols, wired in
``redteam.app.bootstrap``. ``agents``/``settings`` are Agent-Team-internal
and default, so existing call sites keep working.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from redteam.attack_graph.service import AttackGraphService
from redteam.core.contracts.engagement import Engagement
from redteam.core.contracts.specialists import SpecialistRunPolicy
from redteam.core.ports.agent_prompts import AgentPrompts
from redteam.core.ports.artifacts import ArtifactStore
from redteam.core.ports.attack_graph import AttackGraphStore
from redteam.core.ports.control_plane import ControlPlane
from redteam.core.ports.event_bus import EventBus, EventHistory
from redteam.core.ports.evidence_store import EvidenceStore
from redteam.core.ports.execution_recovery import ExecutionRecoveryProvider
from redteam.core.ports.llm import LLMClient
from redteam.core.ports.program_discovery import ShellProgramDiscovery
from redteam.core.ports.task_graph import MissionTaskStore
from redteam.core.ports.tool_catalog import ToolCatalog
from redteam.core.ports.tool_gateway import ToolGateway
from redteam.core.provider_contracts import (
    LEGACY_OPERATION_BINDINGS,
    ExecutionRecipeSpec,
    OperationTargetBinding,
    RetryPolicy,
)
from redteam.orchestration.agents.providers import (
    CandidateProviderRegistry,
    default_provider_registry,
)
from redteam.orchestration.agents.registry import AgentRegistry, default_registry
from redteam.orchestration.analysis import AnalysisVerifier, StructuredLLMAnalysisVerifier
from redteam.orchestration.checkpointing import StateCodecRegistry
from redteam.orchestration.component_binding import RuntimeComponentBinding
from redteam.orchestration.context_projection import ContextProjectionCache
from redteam.orchestration.coordination import Coordinator
from redteam.orchestration.eligibility import EligibilityRegistry, default_eligibility_registry
from redteam.orchestration.local_tools import LocalToolRegistry
from redteam.orchestration.long_jobs import LongJobAdapter
from redteam.orchestration.methodology import ReferenceLibrary
from redteam.orchestration.observations import ResultMapperRegistry, default_result_mapper_registry
from redteam.orchestration.operator_intents.store import OperatorIntentStore
from redteam.orchestration.paths import InMemoryPathStore, PathStore
from redteam.orchestration.planning import PlanningProvider
from redteam.orchestration.settings import OrchestrationSettings
from redteam.orchestration.task_graph.memory import InMemoryTaskGraphStore
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
    verifier: AnalysisVerifier | None = None
    path_store: PathStore = field(default_factory=InMemoryPathStore)
    long_job_adapter: LongJobAdapter | None = None
    execution_recovery: ExecutionRecoveryProvider | None = None
    engagement: Engagement | None = None

    agents: AgentRegistry = field(default_factory=default_registry)
    candidate_providers: CandidateProviderRegistry = field(
        default_factory=default_provider_registry
    )
    # The actual tool registry is mandatory when compiling actions.
    # None supports read-only projections and explicit error scenarios.
    tool_catalog: ToolCatalog | None = None
    methodology_library: ReferenceLibrary | None = None
    # Tool Result -> Attack Graph Mutation (SERVICE/ENDPOINT discovery from
    # a successful execution's real output) -- see `orchestration.observations`.
    result_mappers: ResultMapperRegistry = field(default_factory=default_result_mapper_registry)
    work_store: WorkStore = field(default_factory=InMemoryWorkStore)
    task_graph_store: MissionTaskStore = field(default_factory=InMemoryTaskGraphStore)
    operator_intent_store: OperatorIntentStore | None = None
    settings: OrchestrationSettings = field(default_factory=OrchestrationSettings)
    context_cache: ContextProjectionCache = field(
        default_factory=ContextProjectionCache, compare=False
    )
    # DefaultControlPlane owns mutable rate/budget counters. Policy admission
    # is intentionally serialized while approved tool calls may run in parallel.
    policy_lock: asyncio.Lock = field(default_factory=asyncio.Lock, compare=False)
    # Serialized by policy_lock. Once one worker in a batch requests approval,
    # siblings defer without minting uncheckpointed/orphan Approvals.
    batch_pending_approvals: set[str] = field(default_factory=set, compare=False)
    #: Bounds how many approved tool invocations may execute at once. One
    #: shared semaphore per run, wired by bootstrap from the engagement's
    #: ``RateLimitPolicy.max_concurrent_actions``. ``None`` (default) leaves
    #: execution unbounded, preserving the behaviour before this knob was
    #: enforced. The bound is applied at the tool call site (EXECUTE), not at
    #: policy admission, so a bounded fan-out batch still executes fully.
    tool_semaphore: asyncio.Semaphore | None = field(default=None, compare=False)
    #: Which registered component set this run was built from.
    component_binding: RuntimeComponentBinding | None = field(default=None, compare=False)
    planning_provider: PlanningProvider | None = field(default=None, compare=False)
    state_codecs: StateCodecRegistry | None = field(default=None, compare=False)
    coordinator: Coordinator | None = field(default=None, compare=False)
    event_history: EventHistory | None = field(default=None, compare=False)
    eligibility: EligibilityRegistry = field(
        default_factory=default_eligibility_registry, compare=False
    )
    operation_bindings: dict[str, OperationTargetBinding] = field(
        default_factory=lambda: {row.operation: row for row in LEGACY_OPERATION_BINDINGS},
        compare=False,
    )
    execution_recipes: dict[str, ExecutionRecipeSpec] = field(default_factory=dict, compare=False)
    retry_policy: RetryPolicy | None = field(default=None, compare=False)
    local_tools: LocalToolRegistry = field(default_factory=LocalToolRegistry, compare=False)
    #: Shell program discovery (TASK-004 cards over the TASK-003 provider).
    #: None until the composition root injects the provider (TASK-008);
    #: discovery reports the unwired status explicitly instead of hiding it.
    shell_programs: ShellProgramDiscovery | None = field(default=None, compare=False)
    specialist_policy: SpecialistRunPolicy | None = None
    agent_prompts: AgentPrompts | None = field(default=None, compare=False)
    artifact_store: ArtifactStore | None = field(default=None, compare=False)

    def __post_init__(self) -> None:
        if self.settings.llm_request_policy is not None:
            from redteam.orchestration.coordination import coordinator_for
            from redteam.orchestration.metered_llm import configure_request_metering

            coordinator = self.coordinator or coordinator_for(
                self.task_graph_store, self.path_store,
            )
            object.__setattr__(self, "llm_client", configure_request_metering(
                self.llm_client, self.settings, coordinator,
            ))
        if self.verifier is None:
            object.__setattr__(
                self,
                "verifier",
                StructuredLLMAnalysisVerifier(
                    self.llm_client,
                    self.evidence_store,
                    AttackGraphService(self.graph_store),
                ),
            )
