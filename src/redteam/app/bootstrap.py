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

import logging
from datetime import datetime, timezone
from pathlib import Path

from redteam.attack_graph.stores.memory import InMemoryAttackGraphStore
from redteam.capability.adapters.mcp.example import ExampleMCPAdapter
from redteam.capability.gateway import MCPToolGateway
from redteam.capability.registry import MCPToolRegistry
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
from redteam.core.ports.tool_gateway import ToolGateway
from redteam.event_bus.memory import InMemoryEventBus
from redteam.evidence.stores.filesystem import FilesystemEvidenceStore
from redteam.llm.fake import FakeLLMClient
from redteam.orchestration.context import RuntimeDeps
from redteam.orchestration.graph import build_graph
from redteam.orchestration.state import RuntimeState


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


async def _build_default_tool_gateway(config: CapabilityConfig) -> ToolGateway:
    """The one dummy adapter this stage ships, wrapped in a real gateway.

    TODO(capability-team): use ``config.mcp_servers`` to select/instantiate
    real ``MCPAdapter``s here instead of always defaulting to
    ``ExampleMCPAdapter`` — the config value already reaches this call
    site, nothing upstream needs to change once real adapters exist.
    """
    registry = MCPToolRegistry()
    example_adapter = ExampleMCPAdapter()
    for descriptor in await example_adapter.discover_tools():
        registry.register(descriptor)
    return MCPToolGateway(registry, {example_adapter.server_id: example_adapter})


async def build_runtime_deps(
    engagement: Engagement,
    evidence_dir: str | Path = "data/evidence",
    audit_dir: str | Path = "data/audit",
    approval_dir: str | Path = "data/approvals",
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
    tool_gateway = await _build_default_tool_gateway(CapabilityConfig())
    audit_log = AuditLog(FilesystemAuditStore(audit_dir))
    approvals = ApprovalService(FilesystemApprovalStore(approval_dir))
    control_plane = DefaultControlPlane(
        {engagement.engagement_id: engagement}, audit_log=audit_log, approvals=approvals
    )
    llm_client = FakeLLMClient()

    return RuntimeDeps(
        control_plane=control_plane,
        tool_gateway=tool_gateway,
        event_bus=event_bus,
        graph_store=graph_store,
        evidence_store=evidence_store,
        llm_client=llm_client,
    )


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
) -> tuple[Engagement, RuntimeDeps]:
    """``AppConfig``-aware composition root. Every dependency is overridable.

    ``AppConfig`` sections reach their component like this:

    - ``config.engagement`` -> ``Engagement`` handed to the default
      ``ControlPlane`` (and returned alongside ``RuntimeDeps`` — callers
      pass it to ``run_engagement``).
    - ``config.llm`` -> ``_build_llm_client`` (``FakeLLMClient`` unless
      ``provider: "litellm"``).
    - ``config.capability`` -> ``_build_default_tool_gateway``.
    - ``config.orchestration`` -> not consumed here; pass it to
      ``run_engagement(..., orchestration_config=config.orchestration)``
      so ``RuntimeState.remaining_budget`` reflects ``max_steps``.
    - ``config.logging`` -> ``logging.basicConfig`` below.

    Passing an explicit keyword (e.g. ``control_plane=FakeControlPlane()``)
    skips building that default entirely — this is what lets tests
    inject ``tests.fakes`` doubles for some ports while still exercising
    real config-driven wiring for others.
    """
    logging.basicConfig(level=config.logging.level)

    engagement = config.engagement.to_engagement(engagement_id=EngagementId(new_id("eng")))

    resolved_control_plane = control_plane or DefaultControlPlane(
        {engagement.engagement_id: engagement},
        audit_log=AuditLog(FilesystemAuditStore(audit_dir)),
        approvals=ApprovalService(FilesystemApprovalStore(approval_dir)),
    )
    resolved_tool_gateway = tool_gateway or await _build_default_tool_gateway(config.capability)
    resolved_evidence_store = evidence_store or FilesystemEvidenceStore(evidence_dir)
    resolved_event_bus = event_bus or InMemoryEventBus()
    resolved_attack_graph = attack_graph or InMemoryAttackGraphStore()
    resolved_llm = llm or _build_llm_client(config.llm)

    deps = RuntimeDeps(
        control_plane=resolved_control_plane,
        tool_gateway=resolved_tool_gateway,
        event_bus=resolved_event_bus,
        graph_store=resolved_attack_graph,
        evidence_store=resolved_evidence_store,
        llm_client=resolved_llm,
    )
    return engagement, deps


async def run_engagement(
    engagement: Engagement,
    deps: RuntimeDeps | None = None,
    orchestration_config: OrchestrationConfig | None = None,
) -> RuntimeState:
    """Runs the Minimal Vertical Slice (instruction.md section 16) once.

    ``orchestration_config``, when given, sets
    ``RuntimeState.remaining_budget`` from ``max_steps`` — the one place
    ``OrchestrationConfig`` currently reaches Orchestration.
    ``max_retries`` isn't consumed yet (no retry logic exists —
    TODO(agent-team)).
    """
    deps = deps if deps is not None else await build_runtime_deps(engagement)
    run_id = RunId(new_id("run"))

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

    graph = build_graph(deps)
    initial_state = {
        "runtime": RuntimeState(
            run_id=run_id,
            engagement_id=engagement.engagement_id,
            phase=AgentPhase.OBSERVE,
            current_goal="validate architecture wiring end-to-end",
            remaining_budget=orchestration_config.max_steps if orchestration_config else None,
        ),
        "action_proposal": None,
        "policy_decision": None,
        "tool_result": None,
    }
    final_state = await graph.ainvoke(initial_state)
    runtime: RuntimeState = final_state["runtime"]

    await deps.event_bus.publish(
        EventEnvelope(
            event_id=EventId(new_id("evt")),
            event_type=EventType.RUN_FINISHED,
            run_id=run_id,
            engagement_id=engagement.engagement_id,
            source="app.bootstrap",
            timestamp=datetime.now(timezone.utc),
            payload={"phase": runtime.phase.value},
        )
    )
    return runtime
