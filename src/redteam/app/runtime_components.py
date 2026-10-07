"""App-side component registry, resolver, and factories (A2).

Resolution order for every run:

1. Resolve the requested selection against the registry before any MCP
   transport is opened. Unknown id or version fails here.
2. Build the durable stores (evidence, graph, model client, control).
3. Instantiate the registered factory after those stores exist, in the
   owning task of the run. Every call builds fresh objects. Mutable
   registries are never shared between runs.
4. Record the declared component ids as the durable binding of the run.

Explicit objects passed to the composition root win for the live run,
but a durable resume rebuilds from the registry. A stored record whose
ids differ from the registered profile fails instead of falling back.
The model client is not a profile component.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any
from uuid import uuid4

from redteam.app.extensions import ExtensionAssembly, ExtensionAssemblyError, collect_extensions
from redteam.orchestration.component_binding import (
    AGENTS_COMPONENT,
    BUILTIN_COMPONENT_IDS,
    BUILTIN_PROFILE_ID,
    BUILTIN_PROFILE_VERSION,
    CANDIDATE_PROVIDERS_COMPONENT,
    COMPONENT_SLOTS,
    RESULT_MAPPERS_COMPONENT,
    VERIFIER_COMPONENT,
    ComponentBindingError,
    ComponentProfile,
    ComponentRegistration,
    ComponentRegistry,
    NonRebuildableComponentsError,
    RuntimeComponentBinding,
    binding_for_registration,
    builtin_component_binding,
)


@dataclass(frozen=True)
class ComponentFactoryContext:
    """Stores handed to a factory at instantiation time."""

    llm_client: Any
    evidence_store: Any
    graph_store: Any


@dataclass(frozen=True)
class PlanningFactoryContext:
    """Inference procedures get model access on each budgeted request."""

    provider_id: str


@dataclass(frozen=True)
class RuntimeComponents:
    """One run freshly built agent side implementations."""

    agents: Any
    candidate_providers: Any
    verifier: Any
    result_mappers: Any
    planning_provider: Any = None
    eligibility: Any = None
    operation_bindings: Any = None
    execution_recipes: Any = None
    retry_policy: Any = None
    local_tools: Any = None


RuntimeComponentFactory = Callable[[ComponentFactoryContext], RuntimeComponents]


def _builtin_factory(context: ComponentFactoryContext) -> RuntimeComponents:
    """Build the canonical builtin set. Fresh objects on every call."""
    from redteam.attack_graph.service import AttackGraphService
    from redteam.orchestration.agents.providers import CandidateProviderRegistry
    from redteam.orchestration.agents.registry import AgentRegistry
    from redteam.orchestration.analysis import StructuredLLMAnalysisVerifier
    from redteam.orchestration.eligibility import EligibilityRegistry
    from redteam.orchestration.observations import ResultMapperRegistry

    components = RuntimeComponents(
        agents=AgentRegistry(),
        candidate_providers=CandidateProviderRegistry(),
        verifier=StructuredLLMAnalysisVerifier(
            context.llm_client,
            context.evidence_store,
            AttackGraphService(context.graph_store),
        ),
        result_mappers=ResultMapperRegistry(),
        eligibility=EligibilityRegistry(),
    )
    return apply_extensions(
        components, context, collect_extensions(plugins=(_BuiltinRuntimeExtension(),))
    )


class _BuiltinRuntimeExtension:
    from redteam.core.extensions import hookimpl

    @hookimpl
    def redteam_extensions(self):
        from redteam.capability.action_binding import (
            extension_contributions as action_contributions,
        )
        from redteam.capability.execution_recipes import (
            extension_contributions as recipe_contributions,
        )
        from redteam.core.extensions import ExtensionDeclaration
        from redteam.orchestration.agents.packages import builtin_extension_declaration as roles
        from redteam.orchestration.analysis_activation import (
            builtin_extension_declaration as eligibility,
        )
        from redteam.orchestration.authentication_local_tools import (
            authentication_assessment_contribution,
        )
        from redteam.orchestration.graph_recording import graph_recording_contribution
        from redteam.orchestration.job_local_tools import job_reconciliation_contribution
        from redteam.orchestration.native_local_tools import (
            native_local_tool_contributions,
        )
        from redteam.orchestration.observations.packages import (
            builtin_extension_declaration as observations,
        )

        return (
            roles(),
            observations(),
            eligibility(),
            ExtensionDeclaration(
                "builtin.action-data",
                "1",
                (*action_contributions(), *recipe_contributions()),
            ),
            ExtensionDeclaration("builtin.graph-recording", "1", (graph_recording_contribution(),)),
            ExtensionDeclaration(
                "builtin.native-local-tools",
                "1",
                native_local_tool_contributions(),
            ),
            ExtensionDeclaration(
                "builtin.authentication-assessment", "1",
                (authentication_assessment_contribution(),),
            ),
            ExtensionDeclaration(
                "builtin.job-reconciliation", "1", (job_reconciliation_contribution(),),
            ),
        )


def default_component_registry() -> ComponentRegistry:
    """Return a fresh registry holding only the canonical builtin profile."""
    registry = ComponentRegistry()
    registry.register(
        ComponentProfile(
            profile_id=BUILTIN_PROFILE_ID,
            version=BUILTIN_PROFILE_VERSION,
        ),
        component_ids=dict(BUILTIN_COMPONENT_IDS),
        factory=_builtin_factory,
    )
    return registry


def resolve_selection(
    selection: ComponentProfile | None = None,
    *,
    registry: ComponentRegistry | None = None,
) -> ComponentRegistration:
    """Resolve before any MCP transport is opened. Unknown fails here."""
    active = registry if registry is not None else default_component_registry()
    return active.resolve(selection)


def binding_for_resume(
    stored: RuntimeComponentBinding | None,
    *,
    registry: ComponentRegistry | None = None,
    extensions: ExtensionAssembly | None = None,
) -> RuntimeComponentBinding:
    """Return the binding to rebuild a run from.

    A missing record is a legacy checkpoint and resumes as builtin. An
    unknown id or version, or changed component ids, is an error. There
    is no fallback to another version or to the defaults.
    """
    if stored is None:
        return builtin_component_binding()
    active = registry if registry is not None else default_component_registry()
    active.resolve_binding(stored)
    try:
        assembly = (
            extensions
            if extensions is not None
            else collect_extensions(stored.extension_entry_points)
        )
    except ExtensionAssemblyError as exc:
        raise ComponentBindingError(str(exc)) from exc
    if assembly.binding() != stored.extension_versions:
        raise ComponentBindingError("stored extensions differ from the selected declarations")
    if stored.planning_provider_id is not None:
        try:
            assembly.resolve("planning", stored.planning_provider_id)
        except ExtensionAssemblyError as exc:
            raise ComponentBindingError(str(exc)) from exc
    return stored


def instantiate(
    registration: ComponentRegistration,
    context: ComponentFactoryContext,
) -> tuple[RuntimeComponents, RuntimeComponentBinding]:
    """Call the factory after the stores exist. Return components and record."""
    components = registration.factory(context)
    if not isinstance(components, RuntimeComponents) or any(
        getattr(components, slot) is None for slot in COMPONENT_SLOTS
    ):
        raise ComponentBindingError("component factory must supply all four runtime components")
    return components, binding_for_registration(registration)


def apply_extensions(
    components: RuntimeComponents,
    context: ComponentFactoryContext,
    assembly: ExtensionAssembly,
    *,
    planning_provider_id: str | None = None,
    available_references=(),
) -> RuntimeComponents:
    """Connect partial contributions to the same existing runtime registries."""
    available = [
        *available_references,
        *(("candidate_provider", key) for key in components.candidate_providers.provider_ids),
        *(("agent_definition", str(key)) for key in components.agents.definition_ids),
        *(("agent_role", str(key)) for key in components.agents.definition_ids),
        *(("agent_spec", str(key)) for key in components.agents.agent_ids),
        *(("result_mapper", key) for key in components.result_mappers.all_meaningful_change_keys()),
        *(
            ("eligibility_provider", key)
            for key in (
                components.eligibility.provider_ids if components.eligibility is not None else ()
            )
        ),
        *(
            ("capability.action_binding", "operation:" + key)
            for key in (components.operation_bindings or {})
        ),
        *(
            ("capability.execution_recipe", "recipe:" + key)
            for key in (components.execution_recipes or {})
        ),
    ]
    assembly.validate_references(available)
    assembly.validate_kinds(
        (
            "agent_role",
            "agent_definition",
            "agent_spec",
            "candidate_provider",
            "result_mapper",
            "planning",
            "verifier",
            "eligibility_provider",
            "state_codec",
            "mcp_profile",
            "vendor_policy",
            "reference_bundle",
            "methodology_read_contract",
            "capability.action_binding",
            "capability.retry_policy",
            "capability.execution_recipe",
            "runtime_tool",
        )
    )
    for contribution in assembly.contributions("candidate_provider"):
        components.candidate_providers.register(
            contribution.contribution_id, contribution.factory(context)
        )
    from redteam.orchestration.agents.packages import register_role_package

    for contribution in assembly.contributions("agent_role"):
        package = contribution.factory(context)
        if str(package.agent_id) != contribution.contribution_id:
            raise ExtensionAssemblyError("role package identity differs from its declaration")
        register_role_package(components.agents, components.candidate_providers, package)
    for contribution in assembly.contributions("agent_definition"):
        definition = contribution.factory(context)
        if str(definition.agent_id) != contribution.contribution_id:
            raise ExtensionAssemblyError("agent definition identity differs from its declaration")
        components.agents.register_definition(definition)
    for contribution in assembly.contributions("agent_spec"):
        spec = contribution.factory(context)
        if str(spec.agent_id) != contribution.contribution_id:
            raise ExtensionAssemblyError(
                "agent specification identity differs from its declaration"
            )
        components.agents.register(spec)
    for contribution in assembly.contributions("result_mapper"):
        mapper = contribution.factory(context)
        for tool_id in contribution.target_ids or (contribution.contribution_id,):
            components.result_mappers.register(
                tool_id, mapper, meaningful_change_keys=contribution.meaningful_change_keys
            )
    from redteam.orchestration.eligibility import default_eligibility_registry

    eligibility = components.eligibility or default_eligibility_registry()
    for contribution in assembly.contributions("eligibility_provider"):
        eligibility.register(contribution.factory(context))
    bindings = dict(components.operation_bindings or {})
    for contribution in assembly.contributions("capability.action_binding"):
        binding = contribution.factory(context)
        if binding.operation in bindings:
            raise ExtensionAssemblyError(f"duplicate operation binding: {binding.operation}")
        bindings[binding.operation] = binding
    recipes = dict(components.execution_recipes or {})
    for contribution in assembly.contributions("capability.execution_recipe"):
        recipe = contribution.factory(context)
        if recipe.name in recipes:
            raise ExtensionAssemblyError(f"duplicate execution recipe: {recipe.name}")
        recipes[recipe.name] = recipe
    retry_policies = assembly.contributions("capability.retry_policy")
    if len(retry_policies) > 1:
        raise ExtensionAssemblyError("select one retry policy")
    verifiers = assembly.contributions("verifier")
    if len(verifiers) > 1:
        raise ExtensionAssemblyError("select one verification implementation")
    for definition in components.agents.definitions():
        if definition.schedulable:
            if components.agents.get(definition.agent_id) is None:
                raise ExtensionAssemblyError(f"missing executable role: {definition.agent_id}")
    planners = assembly.contributions("planning")
    from redteam.orchestration.local_tools import LocalToolRegistry

    local_tools = components.local_tools or LocalToolRegistry()
    for contribution in assembly.contributions("runtime_tool"):
        descriptor, handler = contribution.factory(context)
        local_tools.register(descriptor, handler)
    if planning_provider_id is None and planners:
        raise ExtensionAssemblyError("planning contributions require an explicit selection")
    provider = components.planning_provider
    if planning_provider_id is not None:
        provider = assembly.resolve("planning", planning_provider_id).factory(
            PlanningFactoryContext(planning_provider_id)
        )
    return replace(
        components,
        local_tools=local_tools,
        planning_provider=provider,
        eligibility=eligibility,
        operation_bindings=bindings,
        execution_recipes=recipes,
        retry_policy=retry_policies[0].factory(context)
        if retry_policies
        else components.retry_policy,
        verifier=verifiers[0].factory(context) if verifiers else components.verifier,
    )


def bind_extensions(
    binding: RuntimeComponentBinding,
    assembly: ExtensionAssembly,
    *,
    selected: tuple[str, ...] = (),
    planning_provider_id: str | None = None,
) -> RuntimeComponentBinding:
    return binding.model_copy(
        update={
            "extension_versions": assembly.binding(),
            "extension_entry_points": selected,
            "planning_provider_id": planning_provider_id,
        }
    )


def ensure_durable_binding(
    binding: RuntimeComponentBinding | None,
    *,
    durable: bool,
    agents: Any = None,
    candidate_providers: Any = None,
    verifier: Any = None,
    result_mappers: Any = None,
    registry: ComponentRegistry | None = None,
) -> RuntimeComponentBinding:
    """Check a run binding before the run starts.

    Explicit objects are fine for a live run but a durable resume
    rebuilds from the registry, so explicit objects with durable
    checkpointing is a configuration error. Unknown or changed ids
    are always errors. There is no fallback.
    """
    active = registry if registry is not None else default_component_registry()
    record = binding if binding is not None else builtin_component_binding()
    active.resolve_binding(record)
    overrides = {
        AGENTS_COMPONENT: agents,
        CANDIDATE_PROVIDERS_COMPONENT: candidate_providers,
        VERIFIER_COMPONENT: verifier,
        RESULT_MAPPERS_COMPONENT: result_mappers,
    }
    replaced = {slot for slot, value in overrides.items() if value is not None}
    if durable and replaced:
        raise NonRebuildableComponentsError(
            "durable run received explicit component objects that resume cannot rebuild; "
            "register a component profile and select it instead of passing live objects",
        )
    if not replaced:
        return record
    # These objects can continue in this process but cannot be reconstructed
    # from the selected registered factory in a later process.
    identity = "live-" + uuid4().hex
    return RuntimeComponentBinding(
        profile_id=identity,
        version="1",
        component_ids={
            slot: identity if slot in replaced else component_id
            for slot, component_id in record.component_ids.items()
        },
    )
