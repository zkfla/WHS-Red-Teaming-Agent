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

- ``build_runtime_deps(engagement, ...)`` — the original, ``Engagement``-only
  entry point. Still what the CLI and most tests use; unchanged.
- ``build_application(config, ...)`` — the ``AppConfig``-aware entry
  point. Reads every section of ``AppConfig`` (``LLMConfig`` ->
  ``LLMClient`` choice, ``CapabilityConfig`` -> tool gateway wiring,
  ``LoggingConfig`` -> ``logging.basicConfig``) and takes an explicit
  keyword override for every dependency, so tests can inject Fakes
  instead of any default this function would otherwise build.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path

from redteam.attack_graph.service import AttackGraphService
from redteam.attack_graph.stores.memory import InMemoryAttackGraphStore
from redteam.capability.bootstrap import build_mcp_tool_gateway
from redteam.control.approval import ApprovalService
from redteam.control.audit import AuditLog
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
from redteam.core.enums import AgentPhase
from redteam.core.ids import EngagementId, EventId, RunId, new_id
from redteam.core.ports.attack_graph import AttackGraphStore
from redteam.core.ports.control_plane import ControlPlane
from redteam.core.ports.event_bus import EventBus
from redteam.core.ports.evidence_store import EvidenceStore
from redteam.core.ports.llm import LLMClient
from redteam.core.ports.tool_catalog import ToolCatalog
from redteam.core.ports.tool_gateway import ToolGateway
from redteam.event_bus.memory import InMemoryEventBus
from redteam.evidence.service import EvidenceService
from redteam.evidence.stores.filesystem import FilesystemEvidenceStore
from redteam.llm.fake import FakeLLMClient
from redteam.orchestration.agents.providers import (
    CandidateProviderRegistry,
    default_provider_registry,
)
from redteam.orchestration.agents.registry import AgentRegistry, default_registry
from redteam.orchestration.context import RuntimeDeps
from redteam.orchestration.events import REPORT_PROJECTED
from redteam.orchestration.reporting import ReportingAgent
from redteam.orchestration.runner import AgentLoopRunner
from redteam.orchestration.seed import seed_from_engagement
from redteam.orchestration.settings import OrchestrationSettings
from redteam.orchestration.state import RuntimeState, TerminationReason
from redteam.orchestration.work.memory import InMemoryWorkStore
from redteam.orchestration.work.sqlite import SQLiteWorkStore
from redteam.orchestration.work.store import WorkStore


def load_engagement(path: str | Path) -> Engagement:
    """Load an engagement YAML file into a full ``Engagement``.

    Delegates schema/parsing to ``EngagementConfig``
    (``redteam.core.config``) and only assigns the runtime-only fields
    (``engagement_id``, ``created_at``) here.
    """
    config: EngagementConfig = load_engagement_config(path)
    return config.to_engagement(engagement_id=EngagementId(new_id("eng")))


def _build_llm_client(config: LLMConfig) -> LLMClient:
    if config.provider == "litellm":
        from redteam.llm.litellm import LiteLLMClient

        return LiteLLMClient(model=config.default_model, api_key=config.resolve_api_key())
    return FakeLLMClient()


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


async def build_runtime_deps(
    engagement: Engagement,
    evidence_dir: str | Path = "data/evidence",
    audit_dir: str | Path = "data/audit",
    approval_dir: str | Path = "data/approvals",
    *,
    orchestration_settings: OrchestrationSettings | None = None,
    agents: AgentRegistry | None = None,
    candidate_providers: CandidateProviderRegistry | None = None,
    tool_catalog: ToolCatalog | None = None,
) -> RuntimeDeps:
    """Wire in-memory/dummy backends for every port, from an ``Engagement`` alone.

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
    event_bus = InMemoryEventBus()
    graph_store = InMemoryAttackGraphStore()
    evidence_store = FilesystemEvidenceStore(evidence_dir)
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
    )
    llm_client = FakeLLMClient()
    settings = orchestration_settings or OrchestrationSettings()

    return RuntimeDeps(
        control_plane=control_plane,
        tool_gateway=tool_gateway,
        event_bus=event_bus,
        graph_store=graph_store,
        evidence_store=evidence_store,
        llm_client=llm_client,
        agents=agents if agents is not None else default_registry(),
        candidate_providers=(
            candidate_providers
            if candidate_providers is not None
            else default_provider_registry()
        ),
        tool_catalog=(tool_catalog if tool_catalog is not None else discovered_catalog),
        work_store=_build_work_store(settings),
        settings=settings,
    )


def _build_work_store(settings: OrchestrationSettings) -> WorkStore:
    if settings.work_backend == "sqlite":
        return SQLiteWorkStore(settings.work_path)
    return InMemoryWorkStore()


async def build_application(
    config: AppConfig,
    *,
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
    tool_catalog: ToolCatalog | None = None,
) -> tuple[Engagement, RuntimeDeps]:
    """``AppConfig``-aware composition root. Every dependency is overridable.

    ``AppConfig`` sections reach their component like this:

    - ``config.engagement`` -> ``Engagement`` handed to the default
      ``ControlPlane`` (and returned alongside ``RuntimeDeps`` — callers
      pass it to ``run_engagement``).
    - ``config.llm`` -> ``_build_llm_client`` (``FakeLLMClient`` unless
      ``provider: "litellm"``).
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

    engagement = config.engagement.to_engagement(engagement_id=EngagementId(new_id("eng")))

    # Build the evidence store first so both the Control Plane (blocked
    # actions) and Orchestration (executed results) share one store.
    resolved_evidence_store = evidence_store or FilesystemEvidenceStore(evidence_dir)
    resolved_control_plane = control_plane or DefaultControlPlane(
        {engagement.engagement_id: engagement},
        audit_log=AuditLog(FilesystemAuditStore(audit_dir)),
        approvals=ApprovalService(FilesystemApprovalStore(approval_dir)),
        evidence_service=EvidenceService(resolved_evidence_store),
    )
    resolved_tool_catalog = tool_catalog
    if tool_gateway is None:
        resolved_tool_gateway, discovered_catalog = await _build_default_capability(
            config.capability
        )
        if resolved_tool_catalog is None:
            resolved_tool_catalog = discovered_catalog
    else:
        resolved_tool_gateway = tool_gateway
    resolved_event_bus = event_bus or InMemoryEventBus()
    resolved_attack_graph = attack_graph or InMemoryAttackGraphStore()
    resolved_llm = llm or _build_llm_client(config.llm)
    resolved_settings = orchestration_settings or OrchestrationSettings.from_config(
        config.orchestration
    )

    deps = RuntimeDeps(
        control_plane=resolved_control_plane,
        tool_gateway=resolved_tool_gateway,
        event_bus=resolved_event_bus,
        graph_store=resolved_attack_graph,
        evidence_store=resolved_evidence_store,
        llm_client=resolved_llm,
        agents=agents if agents is not None else default_registry(),
        candidate_providers=(
            candidate_providers
            if candidate_providers is not None
            else default_provider_registry()
        ),
        tool_catalog=resolved_tool_catalog,
        work_store=_build_work_store(resolved_settings),
        settings=resolved_settings,
    )
    return engagement, deps


async def _publish_terminal_artifacts(
    runtime: RuntimeState,
    deps: RuntimeDeps,
    *,
    error_type: str | None = None,
) -> None:
    """Project and publish a report for every terminal outcome."""

    report = await ReportingAgent().project(
        runtime,
        graph=AttackGraphService(deps.graph_store),
        evidence_store=deps.evidence_store,
        work_store=deps.work_store,
    )
    await deps.event_bus.publish(
        EventEnvelope(
            event_id=EventId(new_id("evt")),
            event_type=REPORT_PROJECTED,
            run_id=runtime.run_id,
            engagement_id=runtime.engagement_id,
            source="orchestration.reporting",
            timestamp=datetime.now(timezone.utc),
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
    )
    await deps.event_bus.publish(
        EventEnvelope(
            event_id=EventId(new_id("evt")),
            event_type=EventType.RUN_FINISHED,
            run_id=runtime.run_id,
            engagement_id=runtime.engagement_id,
            source="app.bootstrap",
            timestamp=datetime.now(timezone.utc),
            payload={
                "phase": runtime.phase.value,
                "termination_reason": runtime.termination_reason,
                "pending_approval_id": runtime.pending_approval_id,
                "actions_executed": runtime.actions_executed,
                "steps": runtime.step_count,
                "error_type": error_type,
            },
        )
    )


async def run_engagement(
    engagement: Engagement,
    deps: RuntimeDeps | None = None,
    orchestration_config: OrchestrationConfig | None = None,
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
    deps = deps if deps is not None else await build_runtime_deps(engagement)
    settings = deps.settings
    if orchestration_config is not None:
        settings = settings.model_copy(
            update={
                "max_steps": orchestration_config.max_steps,
                "max_retries": orchestration_config.max_retries,
            }
        )
    run_id = RunId(new_id("run"))

    await seed_from_engagement(AttackGraphService(deps.graph_store), engagement, run_id=run_id)

    await deps.event_bus.publish(
        EventEnvelope(
            event_id=EventId(new_id("evt")),
            event_type=EventType.RUN_STARTED,
            run_id=run_id,
            engagement_id=engagement.engagement_id,
            source="app.bootstrap",
            timestamp=datetime.now(timezone.utc),
            payload={"engagement_id": engagement.engagement_id},
        )
    )

    initial_runtime = RuntimeState(
        run_id=run_id,
        engagement_id=engagement.engagement_id,
        phase=AgentPhase.OBSERVE,
        current_goal=f"map and probe the declared scope of {engagement.name}",
        remaining_budget=engagement.execution_budget.max_actions,
    )
    runner = AgentLoopRunner(deps, settings=settings)
    try:
        try:
            runtime = await runner.run(initial_runtime)
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
                    )
                )
            except Exception:  # noqa: BLE001 - never mask the run failure
                pass
            raise
        await _publish_terminal_artifacts(runtime, deps)
        return runtime
    finally:
        await runner.aclose()
