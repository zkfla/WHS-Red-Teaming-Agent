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
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

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
class RuntimeComponents:
    """One run freshly built agent side implementations."""

    agents: Any
    candidate_providers: Any
    verifier: Any
    result_mappers: Any


RuntimeComponentFactory = Callable[[ComponentFactoryContext], RuntimeComponents]


def _builtin_factory(context: ComponentFactoryContext) -> RuntimeComponents:
    """Build the canonical builtin set. Fresh objects on every call."""
    from redteam.attack_graph.service import AttackGraphService
    from redteam.orchestration.agents.providers import default_provider_registry
    from redteam.orchestration.agents.registry import default_registry
    from redteam.orchestration.analysis import StructuredLLMAnalysisVerifier
    from redteam.orchestration.observations import default_result_mapper_registry

    return RuntimeComponents(
        agents=default_registry(),
        candidate_providers=default_provider_registry(),
        verifier=StructuredLLMAnalysisVerifier(
            context.llm_client,
            context.evidence_store,
            AttackGraphService(context.graph_store),
        ),
        result_mappers=default_result_mapper_registry(),
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
