"""Composition root: wires concrete implementations to ``core`` ports.

The only place in the codebase where concrete Control/Evidence/
Capability/Orchestration/LLM implementations are imported together and
connected — via plain constructor injection, no DI framework
(instruction.md section 19).

``RuntimeDeps`` (``redteam.orchestration.context.RuntimeDeps``) plays
the role instruction.md section 21 calls ``ApplicationServices`` — kept
under its existing name rather than renamed, since it's already wired
through every orchestration node and this stage avoids no-op renames
(instruction.md section 29 priority 2: preserve existing architecture).

Two entry points build a ``RuntimeDeps``:

- ``build_application(config, ...)`` — the ``AppConfig``-aware entry
  point, and the canonical one: reads every section of ``AppConfig``
  (``LLMConfig`` -> ``LLMClient`` choice, ``CapabilityConfig`` -> tool
  gateway wiring, ``LoggingConfig`` -> ``logging.basicConfig``) and takes
  an explicit keyword override for every dependency, so tests can inject
  Fakes instead of any default this function would otherwise build.
  ``redteam.app.cli`` uses this path exclusively — a real configured LLM
  provider or MCP server reaches a `run` invocation only through here.
- ``build_runtime_deps(engagement, ...)`` — the original, ``Engagement``-only
  composition helper. It requires an explicitly supplied LLM client;
  test doubles are injected by tests, never selected automatically.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import AsyncExitStack
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from redteam.app.methodology import load_reference_library, wire_reference_agent
from redteam.app.report_store import save_terminal_report
from redteam.app.runtime_components import (
    ComponentFactoryContext,
    ComponentProfile,
    ComponentRegistry,
    ensure_durable_binding,
    instantiate,
    resolve_selection,
)
from redteam.attack_graph.service import AttackGraphService
from redteam.attack_graph.stores.memory import InMemoryAttackGraphStore
from redteam.attack_graph.stores.namespaced import EngagementScopedAttackGraphStore
from redteam.attack_graph.stores.sqlite import SQLiteAttackGraphStore
from redteam.capability.bootstrap import build_mcp_tool_gateway, open_mcp_tool_gateway
from redteam.capability.gateway import MCPToolGateway
from redteam.capability.registry import MCPToolRegistry
from redteam.control.approval import ApprovalService
from redteam.control.audit import AuditLog
from redteam.control.risk import RegistryRiskResolver
from redteam.control.service import DefaultControlPlane
from redteam.control.stores.filesystem import FilesystemApprovalStore, FilesystemAuditStore
from redteam.core.config import (
    AppConfig,
    CapabilityConfig,
    EngagementConfig,
    LLMConfig,
    OrchestrationConfig,
    load_engagement_config,
)
from redteam.core.contracts.engagement import Engagement
from redteam.core.contracts.events import EventEnvelope, EventType
from redteam.core.enums import AgentPhase, GraphNodeType, MissionStatus, TaskStatus
from redteam.core.ids import EngagementId, EventId, RunId, new_id
from redteam.core.ports.attack_graph import AttackGraphStore
from redteam.core.ports.control_plane import ControlPlane
from redteam.core.ports.event_bus import EventBus
from redteam.core.ports.evidence_store import EvidenceStore
from redteam.core.ports.llm import LLMClient
from redteam.core.ports.task_graph import MissionTaskStore
from redteam.core.ports.tool_catalog import ToolCatalog
from redteam.core.ports.tool_gateway import ToolGateway
from redteam.event_bus.memory import InMemoryEventBus
from redteam.event_bus.publisher import EventPublisher
from redteam.evidence.service import EvidenceService
from redteam.evidence.stores.filesystem import FilesystemEvidenceStore
from redteam.orchestration.agents.providers import (
    CandidateProviderRegistry,
)
from redteam.orchestration.agents.registry import AgentRegistry
from redteam.orchestration.analysis import AnalysisVerifier
from redteam.orchestration.component_binding import binding_for_registration
from redteam.orchestration.context import RuntimeDeps
from redteam.orchestration.coordination import (
    ChangeSet,
    CoordinationError,
    coordinator_for,
    validate_configuration,
)
from redteam.orchestration.events import REPORT_PROJECTED
from redteam.orchestration.mission_bootstrap import bootstrap_mission
from redteam.orchestration.observations import ResultMapperRegistry
from redteam.orchestration.operator_intents import build_operator_intent_store
from redteam.orchestration.outbox import deliver_pending
from redteam.orchestration.replanning import finalize_mission
from redteam.orchestration.reporting import ReportingAgent
from redteam.orchestration.runner import AgentLoopRunner
from redteam.orchestration.seed import seed_from_engagement
from redteam.orchestration.settings import OrchestrationSettings
from redteam.orchestration.state import RuntimeState, TerminationReason
from redteam.orchestration.task_graph.memory import InMemoryTaskGraphStore
from redteam.orchestration.task_graph.sqlite import SQLiteTaskGraphStore
from redteam.orchestration.work.memory import InMemoryWorkStore
from redteam.orchestration.work.sqlite import SQLiteWorkStore
from redteam.orchestration.work.store import WorkStore

logger = logging.getLogger(__name__)

#: The one, explicit, self-documenting way to request the in-process
#: ExampleMCPAdapter test/dev fixture from `capability.mcp_servers` --
#: never chosen automatically (see `build_application`'s tool-gateway
#: resolution and `_empty_tool_gateway`).
EXAMPLE_MCP_SENTINEL = "example"


def load_engagement(path: str | Path) -> Engagement:
    """Load an engagement YAML file into a full ``Engagement``.

    Delegates schema/parsing to ``EngagementConfig``
    (``redteam.core.config``) and only assigns the runtime-only fields
    (``engagement_id``, ``created_at``) here.
    """
    config: EngagementConfig = load_engagement_config(path)
    return config.to_engagement(engagement_id=EngagementId(new_id("eng")))


def build_llm_client(config: LLMConfig) -> LLMClient:
    """Build the mandatory real model; test clients require explicit injection."""
    if config.provider == "litellm":
        from redteam.llm.litellm import LiteLLMClient

        return LiteLLMClient(model=config.default_model, api_key=config.resolve_api_key())
    raise ValueError(
        f"production requires a real LLM provider, received {config.provider!r}; "
        "configure 'litellm' (test clients must be explicitly injected)"
    )


async def _build_default_capability(
    config: CapabilityConfig,
) -> tuple[ToolGateway, ToolCatalog]:
    """Build one configured gateway and expose its read-only tool catalog."""
    gateway = await build_mcp_tool_gateway(config.mcp_servers)
    return gateway, gateway


async def _build_default_tool_gateway(config: CapabilityConfig) -> ToolGateway:
    """Compatibility helper for callers that only need execution."""
    gateway, _ = await _build_default_capability(config)
    return gateway


def _empty_tool_gateway() -> MCPToolGateway:
    """A real, zero-tool ``MCPToolGateway`` -- no adapters, nothing to invoke.

    ``build_application`` (the real ``redteam run``/CLI production path)
    uses this instead of ``capability.bootstrap.build_mcp_tool_gateway([])``
    when no MCP server is configured, so an unconfigured deployment gets an
    honest empty catalog (visible via ``redteam tools list`` / the
    ``AGENT_NO_EXECUTABLE_CANDIDATES`` event) rather than the harmless
    in-process ``ExampleMCPAdapter`` silently standing in as if a real
    tool had run. ``ExampleMCPAdapter`` itself is unchanged and remains
    available exactly as before via an explicit
    ``build_mcp_tool_gateway([])``/``build_runtime_deps`` call -- both are
    test/example fixtures, never something this function reaches for.
    """
    return MCPToolGateway(MCPToolRegistry(), {})


async def build_runtime_deps(
    engagement: Engagement,
    evidence_dir: str | Path = "data/evidence",
    audit_dir: str | Path = "data/audit",
    approval_dir: str | Path = "data/approvals",
    *,
    llm: LLMClient,
    orchestration_settings: OrchestrationSettings | None = None,
    agents: AgentRegistry | None = None,
    candidate_providers: CandidateProviderRegistry | None = None,
    component_profile: ComponentProfile | None = None,
    component_registry: ComponentRegistry | None = None,
    verifier: AnalysisVerifier | None = None,
    result_mappers: ResultMapperRegistry | None = None,
    tool_catalog: ToolCatalog | None = None,
    task_graph_store: MissionTaskStore | None = None,
) -> RuntimeDeps:
    """Wire in-memory/dummy backends for every port, from an ``Engagement`` alone.

    A **test/example-only composition helper** -- never a second
    production entry point, and never conflated with ``build_application``
    (the sole production path, used by every CLI command that executes an
    Engagement). It exists for callers that only have a bare ``Engagement``
    and no ``AppConfig`` at all: ``examples/minimal_run.py`` and most
    unit/integration test fixtures. The CLI never calls this function.

    TODO(each-team): swap in your real backend here once it's ready —
    e.g. Capability Team adds real ``MCPAdapter``s to
    ``_build_default_tool_gateway``, Agent Team can swap
    ``InMemoryAttackGraphStore`` for a real one. Control Team's
    persistent audit sink is wired below (``FilesystemAuditStore``) —
    like ``evidence_dir``, ``audit_dir`` is a plain function parameter
    rather than an ``AppConfig``/``LoggingConfig`` field, since it isn't
    part of the frozen Contract v0.1 set and doesn't need to be.

    Prefer ``build_application`` over this function when you have a
    full ``AppConfig`` available — it reads the LLM/Capability/Logging
    sections this function doesn't.
    """
    if llm is None:
        raise ValueError("an explicit LLM client is required")
    component_registration = resolve_selection(
        component_profile, registry=component_registry
    )
    settings = orchestration_settings or OrchestrationSettings()
    component_binding = ensure_durable_binding(
        binding_for_registration(component_registration),
        durable=settings.checkpointing and settings.checkpoint_backend == "sqlite",
        agents=agents,
        candidate_providers=candidate_providers,
        verifier=verifier,
        result_mappers=result_mappers,
        registry=component_registry,
    )
    event_bus = InMemoryEventBus()
    # Namespaced even though this builds one fresh store per call, so an
    # Engagement never silently depends on being the store's only tenant —
    # see EngagementScopedAttackGraphStore.
    graph_store = EngagementScopedAttackGraphStore(
        InMemoryAttackGraphStore(), engagement_id=engagement.engagement_id
    )
    evidence_store = FilesystemEvidenceStore(evidence_dir)
    profile_components, _ = instantiate(
        component_registration,
        ComponentFactoryContext(
            llm_client=llm,
            evidence_store=evidence_store,
            graph_store=graph_store,
        ),
    )
    tool_gateway, discovered_catalog = await _build_default_capability(CapabilityConfig())
    audit_log = AuditLog(FilesystemAuditStore(audit_dir))
    approvals = ApprovalService(FilesystemApprovalStore(approval_dir))
    control_plane = DefaultControlPlane(
        {engagement.engagement_id: engagement},
        audit_log=audit_log,
        approvals=approvals,
        # Same evidence_store the Orchestration side writes to, so blocked
        # attempts and executed results land in one place.
        evidence_service=EvidenceService(evidence_store),
        # Risk-tiered approval reads tool risk_level from the Registry.
        risk_resolver=RegistryRiskResolver(discovered_catalog),
    )

    return RuntimeDeps(
        control_plane=control_plane,
        tool_gateway=tool_gateway,
        event_bus=event_bus,
        graph_store=graph_store,
        evidence_store=evidence_store,
        llm_client=llm,
        engagement=engagement,
        agents=agents if agents is not None else profile_components.agents,
        candidate_providers=(
            candidate_providers
            if candidate_providers is not None
            else profile_components.candidate_providers
        ),
        verifier=verifier if verifier is not None else profile_components.verifier,
        result_mappers=(
            result_mappers
            if result_mappers is not None
            else profile_components.result_mappers
        ),
        component_binding=component_binding,
        tool_catalog=(tool_catalog if tool_catalog is not None else discovered_catalog),
        work_store=_build_work_store(settings),
        task_graph_store=(task_graph_store or _build_task_graph_store(settings)),
        path_store=_build_path_store(settings),
        operator_intent_store=build_operator_intent_store(
            settings.operator_intent_backend, settings.operator_intent_path
        ),
        tool_semaphore=asyncio.Semaphore(engagement.rate_limit_policy.max_concurrent_actions),
        settings=settings,
    )


def _build_task_graph_store(settings: OrchestrationSettings) -> MissionTaskStore:
    if settings.task_graph_backend == "sqlite":
        return SQLiteTaskGraphStore(settings.task_graph_path)
    return InMemoryTaskGraphStore()


def _build_path_store(settings: OrchestrationSettings):
    from redteam.orchestration.paths import InMemoryPathStore, SQLitePathStore

    if settings.path_backend == "sqlite" or settings.task_graph_backend == "sqlite":
        return SQLitePathStore(settings.task_graph_path)
    return InMemoryPathStore()


def _build_work_store(settings: OrchestrationSettings) -> WorkStore:
    if settings.work_backend == "sqlite":
        return SQLiteWorkStore(settings.work_path)
    return InMemoryWorkStore()


async def build_application(
    config: AppConfig,
    *,
    mcp_stack: AsyncExitStack | None = None,
    control_plane: ControlPlane | None = None,
    tool_gateway: ToolGateway | None = None,
    evidence_store: EvidenceStore | None = None,
    event_bus: EventBus | None = None,
    attack_graph: AttackGraphStore | None = None,
    llm: LLMClient | None = None,
    evidence_dir: str | Path = "data/evidence",
    audit_dir: str | Path = "data/audit",
    approval_dir: str | Path = "data/approvals",
    orchestration_settings: OrchestrationSettings | None = None,
    agents: AgentRegistry | None = None,
    candidate_providers: CandidateProviderRegistry | None = None,
    component_profile: ComponentProfile | None = None,
    component_registry: ComponentRegistry | None = None,
    verifier: AnalysisVerifier | None = None,
    result_mappers: ResultMapperRegistry | None = None,
    tool_catalog: ToolCatalog | None = None,
    task_graph_store: MissionTaskStore | None = None,
    engagement_id: EngagementId | None = None,
    long_job_adapter=None,
) -> tuple[Engagement, RuntimeDeps]:
    """``AppConfig``-aware composition root. Every dependency is overridable.

    ``AppConfig`` sections reach their component like this:

    - ``config.engagement`` -> ``Engagement`` handed to the default
      ``ControlPlane`` (and returned alongside ``RuntimeDeps`` — callers
      pass it to ``run_engagement``).
    - ``config.llm`` -> ``build_llm_client`` (requires ``provider: "litellm"``).
    - ``config.capability`` -> ``_build_default_tool_gateway``.
    - ``config.orchestration`` -> ``OrchestrationSettings.from_config``;
      an explicit ``orchestration_settings`` may additionally select durable
      checkpoint/work-ledger backends and bounded parallelism.
    - ``config.logging`` -> ``logging.basicConfig`` below.

    Passing an explicit keyword (e.g. ``control_plane=FakeControlPlane()``)
    skips building that default entirely — this is what lets tests
    inject ``tests.fakes`` doubles for some ports while still exercising
    real config-driven wiring for others.
    """
    logging.basicConfig(level=config.logging.level)

    from redteam.app.mcp_deployment import synchronize_deployment

    config = config.model_copy(update={
        "engagement": synchronize_deployment(config.engagement, config.capability.mcp_servers)
    })
    library = (
        load_reference_library(config.capability.methodology)
        if config.capability.methodology is not None
        else None
    )

    # Inspect actual stores before opening any MCP transport or creating a mission.
    resolved_settings = orchestration_settings or OrchestrationSettings.from_config(
        config.orchestration
    )
    resolved_tasks = task_graph_store or _build_task_graph_store(resolved_settings)
    resolved_paths = _build_path_store(resolved_settings)
    resolved_intents = build_operator_intent_store(
        resolved_settings.operator_intent_backend, resolved_settings.operator_intent_path
    )
    coordinator = validate_configuration(
        SimpleNamespace(
            task_graph_store=resolved_tasks,
            path_store=resolved_paths,
            operator_intent_store=resolved_intents,
        )
    )

    async def initialize(change: ChangeSet) -> None:
        pass

    await coordinator.change(initialize)

    resolved_engagement_id = engagement_id or (
        EngagementId(library.engagement_id) if library is not None else EngagementId(new_id("eng"))
    )
    if library is not None and str(resolved_engagement_id) != library.engagement_id:
        raise ValueError("methodology_engagement_mismatch")
    engagement = config.engagement.to_engagement(engagement_id=resolved_engagement_id)
    component_registration = resolve_selection(
        component_profile, registry=component_registry
    )
    component_binding = ensure_durable_binding(
        binding_for_registration(component_registration),
        durable=resolved_settings.checkpointing
        and resolved_settings.checkpoint_backend == "sqlite",
        agents=agents,
        candidate_providers=candidate_providers,
        verifier=verifier,
        result_mappers=result_mappers,
        registry=component_registry,
    )

    # Build the evidence store first so both the Control Plane (blocked
    # actions) and Orchestration (executed results) share one store.
    resolved_evidence_store = evidence_store or FilesystemEvidenceStore(evidence_dir)
    # Resolve the tool gateway before the Control Plane so the latter can
    # read tool risk_level from the Registry for risk-tiered approval.
    resolved_tool_catalog = tool_catalog
    resolved_tool_gateway: ToolGateway
    discovered_catalog: ToolCatalog
    if tool_gateway is None:
        if not config.capability.mcp_servers:
            # No MCP server configured for this run: an honest empty
            # catalog, never the in-process ExampleMCPAdapter (that fallback
            # made a misconfigured/unconfigured deployment look like a real
            # tool had executed -- see `_empty_tool_gateway`).
            discovered_catalog = _empty_tool_gateway()
            resolved_tool_gateway = discovered_catalog
        elif config.capability.mcp_servers == [EXAMPLE_MCP_SENTINEL]:
            # Explicit, deliberate opt-in to the harmless in-process test
            # fixture (never automatic -- see `_empty_tool_gateway`'s
            # docstring). The only way to reach this from a `redteam`
            # subprocess is to actually write this exact value, e.g.
            # `REDTEAM_MCP_SERVERS=example` or `capability.mcp_servers:
            # ["example"]` -- there is no default path that produces it.
            discovered_catalog = await build_mcp_tool_gateway([])
            resolved_tool_gateway = discovered_catalog
        elif mcp_stack is None:
            resolved_tool_gateway, discovered_catalog = await _build_default_capability(
                config.capability
            )
        else:
            # The caller enters/exits this stack in the run's owning task.
            # Keep browser state alive through execution and approval waits.
            scoped_gateway = await mcp_stack.enter_async_context(
                open_mcp_tool_gateway(config.capability.mcp_servers)
            )
            resolved_tool_gateway, discovered_catalog = scoped_gateway, scoped_gateway
        if resolved_tool_catalog is None:
            resolved_tool_catalog = discovered_catalog
    else:
        resolved_tool_gateway = tool_gateway
    resolved_control_plane = control_plane or DefaultControlPlane(
        {engagement.engagement_id: engagement},
        audit_log=AuditLog(FilesystemAuditStore(audit_dir)),
        approvals=ApprovalService(FilesystemApprovalStore(approval_dir)),
        evidence_service=EvidenceService(resolved_evidence_store),
        risk_resolver=(
            RegistryRiskResolver(resolved_tool_catalog)
            if resolved_tool_catalog is not None
            else None
        ),
    )
    resolved_event_bus = event_bus or InMemoryEventBus()
    resolved_settings = orchestration_settings or OrchestrationSettings.from_config(
        config.orchestration
    )
    resolved_attack_graph = attack_graph or EngagementScopedAttackGraphStore(
        SQLiteAttackGraphStore(resolved_settings.attack_graph_path)
        if resolved_settings.attack_graph_backend == "sqlite"
        else InMemoryAttackGraphStore(),
        engagement_id=engagement.engagement_id,
    )
    resolved_llm = llm or build_llm_client(config.llm)
    profile_components, _ = instantiate(
        component_registration,
        ComponentFactoryContext(
            llm_client=resolved_llm,
            evidence_store=resolved_evidence_store,
            graph_store=resolved_attack_graph,
        ),
    )

    resolved_agents = agents if agents is not None else profile_components.agents
    resolved_providers = (
        candidate_providers
        if candidate_providers is not None
        else profile_components.candidate_providers
    )
    if library is not None:
        if (
            library.asset_ref not in engagement.scope.allowed_assets
            or library.asset_ref in engagement.scope.denied_assets
        ):
            raise ValueError("methodology_asset_not_in_approved_scope")
        resolved_agents, resolved_providers = wire_reference_agent(
            library, resolved_tool_catalog, resolved_agents, resolved_providers
        )
    deps = RuntimeDeps(
        control_plane=resolved_control_plane,
        tool_gateway=resolved_tool_gateway,
        event_bus=resolved_event_bus,
        graph_store=resolved_attack_graph,
        evidence_store=resolved_evidence_store,
        llm_client=resolved_llm,
        engagement=engagement,
        agents=resolved_agents,
        candidate_providers=resolved_providers,
        methodology_library=library,
        verifier=verifier if verifier is not None else profile_components.verifier,
        result_mappers=(
            result_mappers
            if result_mappers is not None
            else profile_components.result_mappers
        ),
        component_binding=component_binding,
        tool_catalog=resolved_tool_catalog,
        work_store=_build_work_store(resolved_settings),
        task_graph_store=resolved_tasks,
        path_store=resolved_paths,
        long_job_adapter=long_job_adapter,
        operator_intent_store=resolved_intents,
        tool_semaphore=asyncio.Semaphore(engagement.rate_limit_policy.max_concurrent_actions),
        settings=resolved_settings,
    )
    from redteam.orchestration.mobile_mcp_binding import wire_mobile_mcp
    deps = wire_mobile_mcp(deps)
    return engagement, deps


async def _publish_terminal_artifacts(
    runtime: RuntimeState,
    deps: RuntimeDeps,
    *,
    error_type: str | None = None,
    causation_id: str | None = None,
    publish_run_finished: bool = True,
) -> None:
    """Project and publish a report for every terminal outcome."""

    path = await deps.path_store.get_active(str(runtime.run_id))
    required_hypotheses = (
        {
            criterion.hypothesis_id
            for step in path.steps
            if step.required
            for criterion in step.completion_criteria
            if criterion.hypothesis_id
        }
        if path
        else set()
    )
    report = await ReportingAgent().project(
        runtime,
        graph=AttackGraphService(deps.graph_store),
        evidence_store=deps.evidence_store,
        work_store=deps.work_store,
        task_store=deps.task_graph_store,
        required_hypothesis_ids=required_hypotheses,
    )
    try:
        # The only thing that makes `redteam status`/`findings`/`report`
        # work once this process exits (see app.report_store) — never lets
        # a persistence failure mask the run's real terminal outcome.
        save_terminal_report(report)
    except OSError:
        logger.exception("Failed to persist terminal report for run %s", runtime.run_id)
    report_event = EventEnvelope(
        event_id=EventId(new_id("evt")),
        event_type=REPORT_PROJECTED,
        run_id=runtime.run_id,
        engagement_id=runtime.engagement_id,
        source="orchestration.reporting",
        timestamp=datetime.now(timezone.utc),
        correlation_id=str(runtime.run_id),
        causation_id=causation_id,
        payload={
            "ready_for_release": report.ready_for_release,
            "finding_count": report.finding_count,
            "evidence_count": report.evidence_count,
            "integrity_blockers": report.integrity_blockers,
            "unresolved_hypotheses": report.unresolved_hypotheses,
            "outstanding_cleanup": report.outstanding_cleanup,
            "pending_approval_id": report.pending_approval_id,
            "effectiveness": report.effectiveness.model_dump(mode="json"),
        },
    )
    await deps.event_bus.publish(report_event)
    if not publish_run_finished:
        # Approval is a suspension: the partial projection remains inspectable,
        # but a RUN_FINISHED event would falsely end the same resumable run.
        return
    run_finished = EventEnvelope(
        event_id=EventId(new_id("evt")),
        event_type=EventType.RUN_FINISHED,
        run_id=runtime.run_id,
        engagement_id=runtime.engagement_id,
        source="app.bootstrap",
        timestamp=datetime.now(timezone.utc),
        correlation_id=str(runtime.run_id),
        causation_id=report_event.event_id,
        payload={
            "phase": runtime.phase.value,
            "termination_reason": runtime.termination_reason,
            "pending_approval_id": runtime.pending_approval_id,
            "actions_executed": runtime.actions_executed,
            "steps": runtime.step_count,
            "error_type": error_type,
            "mission_id": runtime.mission_id,
            "mission_status": runtime.mission_status.value if runtime.mission_status else None,
            "planning_diagnostic": runtime.planning_diagnostic.model_dump(mode="json")
            if runtime.planning_diagnostic
            else None,
            "pending_event_count": runtime.pending_event_count,
            "event_delivery_pending": runtime.event_delivery_pending,
        },
    )
    await deps.event_bus.publish(run_finished)


async def run_engagement(
    engagement: Engagement,
    deps: RuntimeDeps | None = None,
    orchestration_config: OrchestrationConfig | None = None,
    *,
    objective: str | None = None,
    run_id: RunId | None = None,
) -> RuntimeState:
    """Runs one engagement: OBSERVE -> ... -> REPLAN, looping until a
    terminal phase (instruction.md section 16, now with real re-planning).

    ``orchestration_config``, when given, feeds
    ``OrchestrationSettings`` (turn ceiling, retry budget) and sets
    ``RuntimeState.remaining_budget``. The engagement's own
    ``ExecutionBudget`` is enforced separately and authoritatively by the
    Control Plane — nothing here can widen it.

    Before the loop starts, the engagement scope is seeded into the
    Attack Graph (``orchestration.seed``): an empty graph means an empty
    frontier, so without this the planner would have nothing to choose
    from on the first turn.
    """
    if deps is None:
        raise ValueError("run_engagement requires dependencies with an explicitly configured LLM")
    from redteam.orchestration.mobile_mcp_binding import wire_mobile_mcp
    deps = wire_mobile_mcp(deps, engagement)
    coordinator = validate_configuration(deps)
    settings = deps.settings
    if orchestration_config is not None:
        settings = settings.model_copy(
            update={
                "max_steps": orchestration_config.max_steps,
                "max_retries": orchestration_config.max_retries,
            }
        )
    library = deps.methodology_library
    run_id = run_id or (RunId(library.run_id) if library is not None else RunId(new_id("run")))
    if library is not None:
        library.check_context(engagement.engagement_id, run_id)
        if (
            library.asset_ref not in engagement.scope.allowed_assets
            or library.asset_ref in engagement.scope.denied_assets
        ):
            raise ValueError("methodology_asset_not_in_approved_scope")
        if await deps.evidence_store.list_for_run(run_id):
            raise ValueError("methodology_run_already_has_evidence_use_resume")

    await seed_from_engagement(AttackGraphService(deps.graph_store), engagement, run_id=run_id)
    if library is not None:
        from redteam.attack_graph.predicates import Confidence, Provenance

        await AttackGraphService(deps.graph_store).upsert_node(
            GraphNodeType.ASSET,
            key=library.asset_ref,
            attrs={"in_scope": True},
            facts={"methodology_reference_ids": [e.reference_id for e in library.entries]},
            provenance=Provenance(
                source="operator:methodology",
                run_id=str(run_id),
                confidence=Confidence.SERVER_DECLARED,
            ),
        )

    # Task-first runtime: open a Mission and seed its initial Task Graph so the
    # Orchestrator schedules Task -> Agent, not Agent first. Additive — a run
    # without this still schedules via agent rotation (the mission_id is then
    # None). Secret-free: only scope metadata and role names are used.
    mission_id = await bootstrap_mission(
        deps.task_graph_store,
        deps.agents,
        engagement,
        objective=objective,
        now=datetime.now(timezone.utc),
    )

    await coordinator.bind(str(run_id), str(mission_id), str(engagement.engagement_id))

    run_started = EventEnvelope(
        event_id=EventId(new_id("evt")),
        event_type=EventType.RUN_STARTED,
        run_id=run_id,
        engagement_id=engagement.engagement_id,
        source="app.bootstrap",
        timestamp=datetime.now(timezone.utc),
        correlation_id=str(run_id),
        payload={"engagement_id": engagement.engagement_id},
    )
    await deps.event_bus.publish(run_started)
    lifecycle = EventPublisher(
        deps.event_bus,
        run_id=run_id,
        engagement_id=engagement.engagement_id,
        source="app.bootstrap",
    )
    mission_created = await lifecycle.publish(
        EventType.MISSION_CREATED,
        {"mission_id": mission_id},
        causation_id=run_started.event_id,
    )
    bootstrap_event_id = mission_created.event_id
    for task in await deps.task_graph_store.list_tasks(mission_id=mission_id):
        task_created = await lifecycle.publish(
            EventType.TASK_CREATED,
            {"mission_id": mission_id, "task_id": task.task_id},
            causation_id=bootstrap_event_id,
        )
        bootstrap_event_id = task_created.event_id

    saved_mission = await deps.task_graph_store.get_mission(mission_id)
    if saved_mission is None:
        raise RuntimeError("created mission is missing from the task store")
    initial_runtime = RuntimeState(
        run_id=run_id,
        engagement_id=engagement.engagement_id,
        started_at=datetime.now(timezone.utc),
        phase=AgentPhase.OBSERVE,
        mission_id=mission_id,
        current_goal=saved_mission.objective,
        remaining_budget=engagement.execution_budget.max_actions,
        component_binding=deps.component_binding,
    )
    runner = AgentLoopRunner(deps, settings=settings)
    try:
        try:
            runtime = await runner.run(initial_runtime, initial_event_id=bootstrap_event_id)
        except BaseException as exc:
            recovered_runtime = runner.failure_runtime or initial_runtime
            failed_runtime = recovered_runtime.model_copy(
                update={
                    "phase": AgentPhase.FAILED,
                    "termination_reason": (
                        TerminationReason.USER_INTERRUPTED
                        if isinstance(exc, asyncio.CancelledError)
                        else TerminationReason.EXECUTION_ERROR
                    ),
                }
            )
            # Preserve the original exception even if reporting infrastructure
            # is also degraded.  Canonical graph/evidence/ledger stores still
            # provide the partial projection whenever this succeeds.
            try:
                await asyncio.shield(
                    _publish_terminal_artifacts(
                        failed_runtime,
                        deps,
                        error_type=type(exc).__name__,
                        causation_id=str(runner.last_event_id) if runner.last_event_id else None,
                    )
                )
            except Exception:  # noqa: BLE001 - never mask the run failure
                pass
            raise
        return await settle_engagement(runtime, deps, last_event_id=runner.last_event_id)
    finally:
        await runner.aclose()


async def settle_engagement(
    runtime: RuntimeState,
    deps: RuntimeDeps,
    *,
    last_event_id: EventId | None = None,
) -> RuntimeState:
    """Project a run outcome, including one resumed after human approval."""
    if runtime.mission_id is not None:
        from redteam.orchestration.operator_intents.service import OperatorIntentService

        service = OperatorIntentService.from_deps(deps)
        if service is not None:
            drained = await service.process_pending(
                mission_id=runtime.mission_id,
                run_id=runtime.run_id,
                publisher=EventPublisher(
                    deps.event_bus,
                    run_id=runtime.run_id,
                    engagement_id=runtime.engagement_id,
                    source="app.bootstrap",
                ),
                last_event_id=last_event_id,
            )
            last_event_id = drained.last_event_id or last_event_id
            newly_paused = any(
                item.applied and item.outcome == "mission_paused" for item in drained.applications
            )
            newly_stopped = any(
                item.applied and item.outcome == "mission_stopped" for item in drained.applications
            )
            if newly_paused or newly_stopped:
                runtime = runtime.model_copy(
                    update={
                        "mission_status": drained.mission_status,
                        "termination_reason": TerminationReason.OPERATOR_PAUSED
                        if newly_paused
                        else TerminationReason.OPERATOR_STOPPED,
                    }
                )
    if runtime.termination_reason == TerminationReason.OPERATOR_PAUSED:
        # A pause is a checkpointed suspension, not a terminal result/report.
        # Leave required Tasks, Mission status and pending Action linkage intact.
        return runtime.model_copy(update={"mission_status": MissionStatus.PAUSED})
    # A pending approval is a suspended run. Settling its active Task would
    # break the exact proposal's resume path.
    if (
        runtime.mission_id is not None
        and runtime.termination_reason != TerminationReason.APPROVAL_PENDING
    ):
        last_event_id = await finalize_mission(
            deps.task_graph_store,
            runtime.mission_id,
            publisher=EventPublisher(
                deps.event_bus,
                run_id=runtime.run_id,
                engagement_id=runtime.engagement_id,
                source="app.bootstrap",
            ),
            last_event_id=last_event_id,
            evidence_store=deps.evidence_store,
            run_id=runtime.run_id,
            llm_client=deps.llm_client,
            decision_max_attempts=deps.settings.decision_max_attempts,
            path_store=deps.path_store,
        )
        mission = await deps.task_graph_store.get_mission(runtime.mission_id)
        if mission is not None:
            updates: dict[str, object] = {"mission_status": mission.status}

            async def terminal_diagnostic(change: ChangeSet):
                return change.records.get(("terminal_review", str(runtime.run_id)))

            raw_diagnostic = await coordinator_for(deps.task_graph_store, deps.path_store).change(
                terminal_diagnostic
            )
            if raw_diagnostic is not None:
                from redteam.orchestration.diagnostics import DecisionDiagnostic

                diagnostic = DecisionDiagnostic.model_validate(raw_diagnostic)
                if runtime.planning_diagnostic is None:
                    updates["planning_diagnostic"] = diagnostic
                    updates["planning_diagnostic_history"] = [
                        *runtime.planning_diagnostic_history,
                        diagnostic,
                    ][-10:]
            # A Mission can end BLOCKED with no blocked Task: an unmet success
            # criterion (for example a target that returned no usable
            # evidence) is not the same as work the plan itself could not
            # reach. Only the latter -- an actual BLOCKED Task -- is reported
            # as MISSION_BLOCKED; otherwise the historical run-level reason
            # (no_actionable_plan) stands and the Mission status/report carry
            # the unmet criteria.
            if (
                mission.status is MissionStatus.BLOCKED
                and runtime.termination_reason == TerminationReason.NO_ACTIONABLE_PLAN
            ):
                tasks = await deps.task_graph_store.list_tasks(mission_id=runtime.mission_id)
                if any(task.status is TaskStatus.BLOCKED for task in tasks):
                    updates["termination_reason"] = TerminationReason.MISSION_BLOCKED
            runtime = runtime.model_copy(update=updates)
    if runtime.mission_id is not None:
        coordinator = coordinator_for(deps.task_graph_store, deps.path_store)
        try:
            pending = await deliver_pending(
                coordinator,
                run_id=str(runtime.run_id),
                engagement_id=str(runtime.engagement_id),
                event_bus=deps.event_bus,
            )
        except CoordinationError:
            # Delivery is best-effort; a binding anomaly must not block the
            # terminal report. Undelivered events stay recoverable (plan 10).
            pending = runtime.pending_event_count
        runtime = runtime.model_copy(
            update={"pending_event_count": pending, "event_delivery_pending": bool(pending)}
        )
        if runtime.termination_reason != TerminationReason.APPROVAL_PENDING:

            async def close(change: ChangeSet):
                binding = change.binding(
                    str(runtime.run_id), str(runtime.mission_id), str(runtime.engagement_id)
                )
                # Reconcilable BLOCKED missions keep their original run identity.
                suspended_or_reconcilable = {
                    TerminationReason.APPROVAL_PENDING,
                    TerminationReason.OPERATOR_PAUSED,
                    TerminationReason.NO_ACTIONABLE_PLAN,
                    TerminationReason.NO_AGENTS,
                    TerminationReason.STALLED,
                    TerminationReason.INCOMPLETE_ACTIVE_WORK,
                    TerminationReason.UNRESOLVED_HYPOTHESES,
                    TerminationReason.CLEANUP_REQUIRED,
                }
                if (
                    runtime.mission_status
                    in {MissionStatus.COMPLETED, MissionStatus.FAILED, MissionStatus.CANCELLED}
                    or runtime.termination_reason not in suspended_or_reconcilable
                ):
                    binding["closed"] = True

            await coordinator.change(close)
    await _publish_terminal_artifacts(
        runtime,
        deps,
        causation_id=str(last_event_id) if last_event_id else None,
        publish_run_finished=runtime.termination_reason != TerminationReason.APPROVAL_PENDING,
    )
    return runtime
