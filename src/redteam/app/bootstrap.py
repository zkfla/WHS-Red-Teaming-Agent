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

``build_application(config, ...)`` is the composition entry point.
It assembles central state, configured providers and the planner/worker runtime.
Tests inject explicit doubles through the same public ports.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping, Sequence
from contextlib import AsyncExitStack
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

from redteam.app.extensions import ExtensionAssembly, ExtensionAssemblyError, collect_extensions
from redteam.app.methodology import (
    bind_reference_library,
    load_reference_library,
    wire_reference_agent,
)
from redteam.app.runtime_components import (
    ComponentFactoryContext,
    ComponentProfile,
    ComponentRegistry,
    RuntimeComponents,
    apply_extensions,
    bind_extensions,
    ensure_durable_binding,
    instantiate,
    resolve_selection,
)
from redteam.attack_graph.stores.central import CentralAttackGraphStore
from redteam.attack_graph.stores.namespaced import EngagementScopedAttackGraphStore
from redteam.capability.adapters.mcp.base import MCPAdapter
from redteam.capability.bootstrap import build_mcp_tool_gateway, open_mcp_tool_gateway
from redteam.capability.gateway import MCPToolGateway
from redteam.capability.registration import (
    CapabilityFactoryContext,
    builtin_mcp_registration_declaration,
)
from redteam.capability.registry import MCPToolRegistry
from redteam.control.approval import ApprovalService
from redteam.control.audit import AuditLog
from redteam.control.network_ledger import SQLiteNetworkGrantLedger, network_activity_in_connection
from redteam.control.risk import RegistryRiskResolver
from redteam.control.service import DefaultControlPlane
from redteam.control.stores.filesystem import FilesystemApprovalStore, FilesystemAuditStore
from redteam.core.config import (
    AppConfig,
    CapabilityConfig,
    EngagementConfig,
    LLMConfig,
    load_engagement_config,
)
from redteam.core.contracts.engagement import Engagement
from redteam.core.contracts.network import NetworkBudget
from redteam.core.environment import env_snapshot
from redteam.core.extensions import hookimpl
from redteam.core.ids import EngagementId, new_id
from redteam.core.ports.artifacts import ArtifactStore
from redteam.core.ports.attack_graph import AttackGraphStore
from redteam.core.ports.code_fixtures import CodeFixtureRunner
from redteam.core.ports.control_plane import ControlPlane
from redteam.core.ports.event_bus import EventBus, EventHistory
from redteam.core.ports.evidence_store import EvidenceStore
from redteam.core.ports.execution_recovery import ExecutionRecoveryProvider
from redteam.core.ports.llm import LLMClient
from redteam.core.ports.program_discovery import ShellProgramDiscovery
from redteam.core.ports.task_graph import MissionTaskStore
from redteam.core.ports.tool_catalog import ToolCatalog
from redteam.core.ports.tool_gateway import ToolGateway
from redteam.event_bus.memory import InMemoryEventBus
from redteam.evidence.service import EvidenceService
from redteam.evidence.stores.filesystem import FilesystemEvidenceStore
from redteam.orchestration.agents.providers import (
    CandidateProviderRegistry,
)
from redteam.orchestration.agents.registry import AgentRegistry
from redteam.orchestration.analysis import AnalysisVerifier
from redteam.orchestration.central_state import CentralStateStore
from redteam.orchestration.checkpointing import (
    StateCodecRegistry,
    consume_state_codec_contributions,
)
from redteam.orchestration.component_binding import binding_for_registration
from redteam.orchestration.context import RuntimeDeps
from redteam.orchestration.coordination import (
    AtomicStateBackend,
)
from redteam.orchestration.observations import ResultMapperRegistry
from redteam.orchestration.operator_intents.store import OperatorIntentStore
from redteam.orchestration.paths import PathStore
from redteam.orchestration.settings import OrchestrationSettings

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from redteam.capability.gated_activation import GatedProviderConfig
    from redteam.capability.shell_runtime import ShellRuntime

#: The one, explicit, self-documenting way to request the in-process
#: ExampleMCPAdapter test/dev fixture from `capability.mcp_servers` --
#: never chosen automatically (see `build_application`'s tool-gateway
#: resolution and `_empty_tool_gateway`).
EXAMPLE_MCP_SENTINEL = "example"


async def build_central_storage(
    engagement: Engagement, *, run_id: str,
    path: str | Path = "data/orchestration/state.sqlite3",
    network_budget: NetworkBudget | None = None,
) -> tuple[CentralStateStore, SQLiteNetworkGrantLedger | None]:
    """Explicit storage-only foundation selection; does not select an agent runtime.

    Callers create/bind their Mission on the returned store. Network accounting
    is constructed only with an explicitly selected existing network budget.
    Application assembly uses this same central owner.
    """
    state = CentralStateStore(
        path, run_id=run_id, engagement_id=str(engagement.engagement_id),
        activity_check=network_activity_in_connection,
    )
    await state.setup()
    network = None
    if network_budget is not None:
        network = SQLiteNetworkGrantLedger(
            state.path, engagement_id=str(engagement.engagement_id), budget=network_budget,
        )
        await network.setup()
    return state, network


def build_central_graph(state: CentralStateStore) -> EngagementScopedAttackGraphStore:
    """Select central graph storage without the legacy cross-database writer fence.

    The supplied state must be initialized and bound. This does not select an
    agent runtime or migrate graph data.
    """
    return EngagementScopedAttackGraphStore(
        CentralAttackGraphStore(state), engagement_id=EngagementId(state.engagement_id),
    )


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
    *,
    profile_bundles=None,
    env=None,
    builtin_adapters: Sequence[MCPAdapter] = (),
    builtin_concurrency: Mapping[str, int] | None = None,
    builtin_output_limits: Mapping[str, int] | None = None,
) -> tuple[ToolGateway, ToolCatalog]:
    """Build one configured gateway and expose its read-only tool catalog."""
    gateway = await build_mcp_tool_gateway(
        config.mcp_servers, profile_bundles=profile_bundles, env=env,
        builtin_adapters=builtin_adapters, builtin_concurrency=builtin_concurrency,
        builtin_output_limits=builtin_output_limits,
    )
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
    ``build_mcp_tool_gateway([])`` call -- it is
    test/example fixtures, never something this function reaches for.
    """
    return MCPToolGateway(MCPToolRegistry(), {})


class _BuiltinMcpProfiles:
    @hookimpl
    def redteam_extensions(self):
        return (builtin_mcp_registration_declaration(),)


def _selected_mcp_profiles(assembly, environment):
    bundles = {}
    context = CapabilityFactoryContext(env=environment)
    builtin = collect_extensions(plugins=(_BuiltinMcpProfiles(),))
    for contribution in (
        *builtin.contributions("mcp_profile"),
        *assembly.contributions("mcp_profile"),
    ):
        bundle = contribution.factory(context)
        if bundle.profile != contribution.contribution_id:
            raise ExtensionAssemblyError("MCP profile differs from its declaration")
        if bundle.profile in bundles:
            raise ExtensionAssemblyError("duplicate MCP profile: " + bundle.profile)
        bundles[bundle.profile] = bundle
    return bundles




def build_selected_code_fixture_runner(specialists, injected, settings) -> CodeFixtureRunner | None:
    """Validate fixture selection and build it without starting Docker/LLM.

    Shared by assembly and offline CLI preflight; construction is not execution.
    """
    path = settings.code_fixture_policy_path
    if injected is None and path is None:
        return None
    if specialists is None or not specialists.code_artifacts_enabled:
        raise ValueError("fixture_requires_selected_code_roles")
    if injected is not None and path is not None:
        raise ValueError("fixture_runner_and_policy_are_ambiguous")
    from redteam.capability.code_fixtures import build_code_fixture_runner

    return injected if injected is not None else build_code_fixture_runner(path)










async def build_application(
    config: AppConfig,
    *,
    mcp_stack: AsyncExitStack | None = None,
    control_plane: ControlPlane | None = None,
    central_state: CentralStateStore | None = None,
    tool_gateway: ToolGateway | None = None,
    evidence_store: EvidenceStore | None = None,
    artifact_store: ArtifactStore | None = None,
    code_fixture_runner: CodeFixtureRunner | None = None,
    event_bus: EventBus | None = None,
    attack_graph: AttackGraphStore | None = None,
    llm: LLMClient | None = None,
    evidence_dir: str | Path = "data/evidence",
    artifact_dir: str | Path | None = None,
    include_code_artifacts: bool = True,
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
    path_store: PathStore | None = None,
    atomic_state_backend: AtomicStateBackend | None = None,
    engagement_id: EngagementId | None = None,
    long_job_adapter=None,
    extensions: ExtensionAssembly | None = None,
    builtin_adapters: Sequence[MCPAdapter] = (),
    builtin_concurrency: Mapping[str, int] | None = None,
    builtin_output_limits: Mapping[str, int] | None = None,
    shell_programs: ShellProgramDiscovery | None = None,
    gated_shell: tuple[ShellRuntime, GatedProviderConfig] | None = None,
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
    selected_extensions = tuple(config.extensions)
    extension_assembly = (
        extensions if extensions is not None else collect_extensions(selected_extensions)
    )
    environment = env_snapshot()
    profile_bundles = _selected_mcp_profiles(extension_assembly, environment)
    state_codecs = StateCodecRegistry()
    consume_state_codec_contributions(
        extension_assembly.contributions("state_codec"), registry=state_codecs
    )
    if config.planning_provider is not None:
        extension_assembly.resolve("planning", config.planning_provider)

    from redteam.app.mcp_deployment import synchronize_deployment

    config = config.model_copy(
        update={
            "engagement": synchronize_deployment(
                config.engagement, config.capability.mcp_servers, env=environment
            )
        }
    )
    library = (
        load_reference_library(config.capability.methodology)
        if config.capability.methodology is not None
        else None
    )

    # Inspect actual stores before opening any MCP transport or creating a mission.
    resolved_settings = orchestration_settings or OrchestrationSettings.from_config(
        config.orchestration
    )
    from redteam.app.agent_profile import selected_specialists

    specialists = selected_specialists(resolved_settings)
    resolved_fixture_runner = build_selected_code_fixture_runner(
        specialists, code_fixture_runner, resolved_settings,
    )
    code_enabled = (
        include_code_artifacts and specialists is not None and specialists.code_artifacts_enabled
    )
    if not code_enabled and resolved_fixture_runner is not None:
        raise ValueError("fixture_requires_enabled_code_artifacts")
    if artifact_store is not None and not code_enabled:
        raise ValueError("artifact_store_requires_selected_code_roles")
    if artifact_store is not None and artifact_dir is None:
        raise ValueError("injected_artifact_store_requires_original_location")
    resolved_artifact_store: ArtifactStore | None = None
    artifact_path = (
        Path(artifact_dir) if artifact_dir is not None else Path(evidence_dir).parent / "artifacts"
    )
    if code_enabled:
        if artifact_path.resolve() in {
            Path(evidence_dir).resolve(), Path(audit_dir).resolve(), Path(approval_dir).resolve(),
        }:
            raise ValueError("artifact_original_store_must_be_separate")
        if artifact_store is None:
            from redteam.evidence.artifacts.filesystem import FilesystemArtifactBackend

            resolved_artifact_store = FilesystemArtifactBackend(artifact_path)
        else:
            resolved_artifact_store = artifact_store
        fingerprint = getattr(resolved_artifact_store, "fingerprint", None)
        if not isinstance(fingerprint, str) or len(fingerprint) != 64:
            raise ValueError("artifact_store_stable_fingerprint_required")
    if central_state is None:
        if specialists is None or resolved_settings.llm_request_policy is None:
            raise ValueError("central_runtime_requires_explicit_specialists_and_request_policy")
        engagement_id = engagement_id or (
            EngagementId(library.engagement_id)
            if library is not None else EngagementId(new_id("eng"))
        )
        resolved_settings = resolved_settings.with_overrides(
            checkpointing=True, checkpoint_backend="sqlite",
        )
        orchestration_settings = resolved_settings
        central_state, _ = await build_central_storage(
            config.engagement.to_engagement(engagement_id), run_id=new_id("run"),
            path=resolved_settings.central_state_path,
        )
    from redteam.core.ports.control_plane import (
        DurableControlRecovery,
        InvocationAdmissionControl,
    )
    central_state.original_store_paths = {
        "evidence": str(Path(evidence_dir).resolve()),
        "audit": str(Path(audit_dir).resolve()), "approvals": str(Path(approval_dir).resolve()),
        "checkpoints": str(Path(resolved_settings.checkpoint_path).resolve()),
    }
    if resolved_artifact_store is not None:
        central_state.original_store_paths["artifacts"] = str(artifact_path.resolve())

    if (specialists is None or resolved_settings.llm_request_policy is None
            or not resolved_settings.checkpointing
            or resolved_settings.checkpoint_backend != "sqlite"):
        raise ValueError("central_selection_requires_specialists_policy_and_sqlite_checkpoint")
    if any(item is not None for item in (task_graph_store, path_store, atomic_state_backend,
                                        attack_graph)):
        raise ValueError("central_selection_cannot_mix_domain_stores")
    if control_plane is not None and not (
        isinstance(control_plane, DurableControlRecovery)
        and isinstance(control_plane, InvocationAdmissionControl)
    ):
        raise ValueError("central_selection_requires_durable_control_and_admission")
    if str(engagement_id) != central_state.engagement_id:
        raise ValueError("central_selection_engagement_identity_mismatch")
    # No duplicate Task/Path/Work containers are constructed for central runs.
    resolved_tasks: MissionTaskStore | None = None
    resolved_intents: OperatorIntentStore = central_state
    resolved_paths: PathStore | None = None
    coordinator = None

    resolved_engagement_id = engagement_id or (
        EngagementId(library.engagement_id) if library is not None else EngagementId(new_id("eng"))
    )
    if library is not None and str(resolved_engagement_id) != library.engagement_id:
        raise ValueError("methodology_engagement_mismatch")
    engagement = config.engagement.to_engagement(engagement_id=resolved_engagement_id)
    component_registration = resolve_selection(component_profile, registry=component_registry)
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
    if gated_shell is not None:
        from redteam.capability.gated_activation import configure_gated

        shell, provider_config = gated_shell
        if tool_gateway is not None:
            raise ValueError("gated provider must be assembled before gateway discovery")
        if not any(adapter is shell.adapter for adapter in builtin_adapters):
            raise ValueError("gated provider must use the registered shell adapter")
        ledger = SQLiteNetworkGrantLedger(
            (central_state.path),
            engagement_id=str(engagement.engagement_id),
            budget=provider_config.network_policy.budget,
        )
        await ledger.setup()
        gated = configure_gated(
            shell, engagement.scope, ledger, resolved_evidence_store,
            provider_config=provider_config,
        )
        shell.adapter.set_gated_policy(gated.executor, gated.descriptors)
    resolved_attack_graph = attack_graph or (build_central_graph(central_state))
    resolved_llm = llm or build_llm_client(config.llm)

    from redteam.orchestration.metered_llm import RunMeteredLLM

    assert specialists is not None and resolved_settings.llm_request_policy is not None
    resolved_llm = RunMeteredLLM(
        resolved_llm, central_state.request_ledger(), prompts=specialists.prompts,
        request_semaphore=asyncio.Semaphore(
            resolved_settings.llm_request_policy.max_concurrent_requests,
        ),
    )
    profile_components, _ = instantiate(
        component_registration,
        ComponentFactoryContext(
            llm_client=resolved_llm,
            evidence_store=resolved_evidence_store,
            graph_store=resolved_attack_graph,
        ),
    )

    profile_components = RuntimeComponents(
        agents=agents if agents is not None else profile_components.agents,
        candidate_providers=candidate_providers
        if candidate_providers is not None
        else profile_components.candidate_providers,
        verifier=verifier if verifier is not None else profile_components.verifier,
        result_mappers=result_mappers
        if result_mappers is not None
        else profile_components.result_mappers,
        planning_provider=profile_components.planning_provider,
        eligibility=profile_components.eligibility,
        operation_bindings=profile_components.operation_bindings,
        execution_recipes=profile_components.execution_recipes,
        retry_policy=profile_components.retry_policy,
        local_tools=profile_components.local_tools,
    )
    profile_components = apply_extensions(
        profile_components,
        ComponentFactoryContext(resolved_llm, resolved_evidence_store, resolved_attack_graph),
        extension_assembly,
        planning_provider_id=config.planning_provider,
        available_references=tuple(("mcp_profile", key) for key in profile_bundles),
    )
    component_binding = bind_extensions(
        component_binding,
        extension_assembly,
        selected=selected_extensions,
        planning_provider_id=config.planning_provider,
    )
    if specialists is not None:
        if agents is not None:
            raise ValueError("specialist_profile_and_explicit_agents_are_ambiguous")
        profile_components = specialists.assemble(
            profile_components, include_code_artifacts=code_enabled,
        )
        component_binding = component_binding.model_copy(update={
            "specialist_fingerprint": specialists.fingerprint,
        })
    # Resolve the tool gateway before the Control Plane so the latter can
    # read tool risk_level from the Registry for risk-tiered approval.
    resolved_tool_catalog = tool_catalog
    resolved_tool_gateway: ToolGateway
    discovered_catalog: ToolCatalog
    if tool_gateway is not None and builtin_adapters:
        raise ValueError("a supplied gateway must already contain its built-in providers")
    if tool_gateway is None:
        if not config.capability.mcp_servers and not builtin_adapters:
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
            if builtin_adapters:
                from redteam.capability.adapters.mcp.example import ExampleMCPAdapter

                discovered_catalog = await build_mcp_tool_gateway(
                    [], builtin_adapters=[ExampleMCPAdapter(), *builtin_adapters],
                    builtin_concurrency=builtin_concurrency,
                    builtin_output_limits=builtin_output_limits,
                )
            else:
                discovered_catalog = await build_mcp_tool_gateway([])
            resolved_tool_gateway = discovered_catalog
        elif mcp_stack is None:
            resolved_tool_gateway, discovered_catalog = await _build_default_capability(
                config.capability, profile_bundles=profile_bundles, env=environment,
                builtin_adapters=builtin_adapters, builtin_concurrency=builtin_concurrency,
                builtin_output_limits=builtin_output_limits,
            )
        else:
            # The caller enters/exits this stack in the run's owning task.
            # Keep browser state alive through execution and approval waits.
            scoped_gateway = await mcp_stack.enter_async_context(
                open_mcp_tool_gateway(
                    config.capability.mcp_servers, profile_bundles=profile_bundles, env=environment,
                    builtin_adapters=builtin_adapters, builtin_concurrency=builtin_concurrency,
                    builtin_output_limits=builtin_output_limits,
                )
            )
            resolved_tool_gateway, discovered_catalog = scoped_gateway, scoped_gateway
        if resolved_tool_catalog is None:
            resolved_tool_catalog = discovered_catalog
    else:
        resolved_tool_gateway = tool_gateway
    if resolved_tool_catalog is not None:
        profile_components.result_mappers.bind_catalog(resolved_tool_catalog)
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
        outbound_descriptor_resolver=(
            resolved_tool_catalog.get if resolved_tool_catalog is not None else None
        ),
    )
    resolved_event_bus = event_bus or InMemoryEventBus()
    event_history = resolved_event_bus if isinstance(resolved_event_bus, EventHistory) else None
    from redteam.orchestration.central_results import register_result_tool
    from redteam.orchestration.central_runtime import CentralEventBus

    register_result_tool(profile_components.local_tools)
    resolved_event_bus = CentralEventBus(central_state, resolved_event_bus)
    component_binding = component_binding.model_copy(update={
        "extension_versions": {**component_binding.extension_versions, "central-runtime": "1"},
    })
    resolved_settings = orchestration_settings or OrchestrationSettings.from_config(
        config.orchestration
    )
    if extension_assembly.contributions("reference_bundle"):
        if library is None:
            raise ExtensionAssemblyError(
                "reference entries require a configured reference workspace"
            )
        entries = {entry.reference_id: entry for entry in library.entries}
        for contribution in extension_assembly.contributions("reference_bundle"):
            entry = contribution.factory(None)
            if entry.reference_id != contribution.contribution_id or entry.reference_id in entries:
                raise ExtensionAssemblyError("duplicate or mismatched reference entry")
            entries[entry.reference_id] = entry
        library = library.model_copy(update={"entries": tuple(entries.values())})
    for contribution in extension_assembly.contributions("methodology_read_contract"):
        if library is None:
            raise ExtensionAssemblyError("reference read contract requires a reference workspace")
        contract = contribution.factory(None)
        if any(
            entry.read_contract.model_dump()
            != {
                "capability": contract.capability,
                "operation": contract.operation,
                "asset_binding": contract.asset_binding,
                "encoding": contract.encoding,
            }
            for entry in library.entries
        ):
            raise ExtensionAssemblyError("reference read contract mismatch")
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
        library = bind_reference_library(library, resolved_tool_catalog)
        resolved_agents, resolved_providers = wire_reference_agent(
            library, resolved_tool_catalog, resolved_agents, resolved_providers
        )
    deps = RuntimeDeps(
        specialist_policy=(specialists.run_policy().model_copy(update={
            "max_instances": resolved_settings.max_workers or specialists.profile.max_instances,
        }) if specialists else None),
        agent_prompts=specialists.prompts if specialists else None,
        artifact_store=resolved_artifact_store,
        code_fixture_runner=resolved_fixture_runner,
        control_plane=resolved_control_plane,
        tool_gateway=resolved_tool_gateway,
        execution_recovery=(
            resolved_tool_gateway
            if isinstance(resolved_tool_gateway, ExecutionRecoveryProvider) else None
        ),
        event_bus=resolved_event_bus,
        event_history=event_history,
        graph_store=resolved_attack_graph,
        evidence_store=resolved_evidence_store,
        llm_client=resolved_llm,
        engagement=engagement,
        agents=resolved_agents,
        candidate_providers=resolved_providers,
        methodology_library=library,
        shell_programs=shell_programs,
        verifier=verifier if verifier is not None else profile_components.verifier,
        result_mappers=(
            result_mappers if result_mappers is not None else profile_components.result_mappers
        ),
        component_binding=component_binding,
        planning_provider=profile_components.planning_provider,
        state_codecs=state_codecs,
        coordinator=coordinator,
        eligibility=profile_components.eligibility,
        operation_bindings=profile_components.operation_bindings,
        execution_recipes=profile_components.execution_recipes,
        retry_policy=profile_components.retry_policy,
        local_tools=profile_components.local_tools,
        tool_catalog=resolved_tool_catalog,
        work_store=(None),
        central_state=central_state,
        task_graph_store=resolved_tasks,
        path_store=resolved_paths,
        long_job_adapter=long_job_adapter,
        operator_intent_store=resolved_intents,
        tool_semaphore=asyncio.Semaphore(engagement.rate_limit_policy.max_concurrent_actions),
        settings=resolved_settings,
    )
    from redteam.capability.role_manifest import configure_role_tools

    role_fingerprint = configure_role_tools(
        resolved_agents.definitions(), resolved_tool_catalog, config.capability.role_manifest_path,
    )
    deps = replace(deps, component_binding=component_binding.model_copy(update={
        "role_tools_fingerprint": role_fingerprint,
        "code_fixture_fingerprint": (deps.code_fixture_runner.fingerprint
                                     if deps.code_fixture_runner is not None else None),
    }))
    if resolved_artifact_store is not None:
        from redteam.orchestration.coordination import content_hash

        assert deps.component_binding is not None
        deps = replace(deps, component_binding=deps.component_binding.model_copy(update={
            "extension_versions": {
                **deps.component_binding.extension_versions,
                "central-code-artifacts": content_hash({
                    "version": "1", "store": resolved_artifact_store.fingerprint,
                }),
            },
        }))
    from redteam.orchestration.mobile_mcp_binding import (
        builtin_mobile_vendor_policies,
        wire_mobile_mcp,
    )

    vendors = [
        *builtin_mobile_vendor_policies(),
        *(item.factory(None) for item in extension_assembly.contributions("vendor_policy")),
    ]
    deps = wire_mobile_mcp(deps, vendor_policies=vendors)
    from redteam.orchestration.central_runtime import tool_limit

    deps = replace(deps, tool_semaphore=asyncio.Semaphore(tool_limit(deps)))
    assert deps.component_binding is not None
    from redteam.orchestration.coordination import content_hash

    input_hash = content_hash({
        "llm": config.llm.model_dump(mode="json"),
        "gated_network": (gated_shell[1].network_policy.model_dump(mode="json")
                          if gated_shell is not None else None),
        "gated_shell": (gated_shell[0].policy.model_dump(mode="json")
                        if gated_shell is not None else None),
        "settings": {
            name: value for name, value in resolved_settings.durable_payload().items()
            if not name.endswith(("_path", "_backend"))
        },
    })
    deps = replace(deps, component_binding=deps.component_binding.model_copy(update={
        "extension_versions": {**deps.component_binding.extension_versions,
                               "central-selection-inputs": input_hash},
    }))
    return engagement, deps










async def run_engagement(
    engagement, *, deps, objective=None, run_id=None, instance_id=None,
    orchestration_config=None,
):
    """Execute central state with the limits fixed by application assembly.

    The former override argument is rejected before entering the run. Pass
    configuration to ``build_application`` when assembling a new run instead.
    Saved runs retain their original settings and component binding.
    """
    if orchestration_config is not None:
        raise ValueError(
            "run_engagement_orchestration_override_removed: pass orchestration "
            "configuration to build_application before starting a new Run"
        )
    from redteam.app.central_run import run_central_engagement
    return await run_central_engagement(
        engagement, deps=deps, objective=objective, run_id=run_id, instance_id=instance_id,
    )


async def settle_engagement(runtime, deps, **kwargs):
    """Settle against current central state and retained originals."""
    from redteam.app.central_run import settle_central_engagement
    return await settle_central_engagement(runtime, deps)
