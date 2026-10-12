"""Minimal checks for the A2 component binding contract."""

from __future__ import annotations

import pytest

from redteam.app.runtime_components import (
    ComponentFactoryContext,
    ComponentProfile,
    RuntimeComponents,
    binding_for_resume,
    default_component_registry,
    ensure_durable_binding,
    instantiate,
    resolve_selection,
)
from redteam.orchestration.component_binding import (
    BUILTIN_COMPONENT_IDS,
    ComponentBindingError,
    ComponentMismatchError,
    DuplicateComponentProfileError,
    NonRebuildableComponentsError,
    UnknownComponentProfileError,
    builtin_component_binding,
    is_builtin_binding,
)


def test_builtin_binding_is_canonical() -> None:
    binding = builtin_component_binding()
    assert binding.profile_id == "builtin"
    assert binding.version == "1"
    assert binding.component_ids == BUILTIN_COMPONENT_IDS
    assert is_builtin_binding(binding)
    assert binding.model_copy() == binding


def test_resolve_unknown_profile_fails() -> None:
    registry = default_component_registry()
    with pytest.raises(UnknownComponentProfileError):
        registry.resolve(ComponentProfile(profile_id="nope", version="9"))


def test_duplicate_registration_fails() -> None:
    registry = default_component_registry()
    reg = registry.resolve(None)
    with pytest.raises(DuplicateComponentProfileError):
        registry.register(
            reg.profile,
            component_ids=dict(reg.component_ids),
            factory=reg.factory,
        )


def test_resume_mismatch_fails_without_fallback() -> None:
    registry = default_component_registry()
    stored = builtin_component_binding().model_copy(
        update={"component_ids": dict(BUILTIN_COMPONENT_IDS, verifier="other-v9")}
    )
    with pytest.raises(ComponentMismatchError):
        binding_for_resume(stored, registry=registry)


def test_legacy_resume_is_builtin() -> None:
    assert binding_for_resume(None) == builtin_component_binding()


def test_durable_with_live_objects_fails_before_start() -> None:
    registry = default_component_registry()
    registration = resolve_selection(None, registry=registry)
    context = ComponentFactoryContext(
        llm_client=object(), evidence_store=object(), graph_store=object()
    )
    _, binding = instantiate(registration, context)
    with pytest.raises(NonRebuildableComponentsError):
        ensure_durable_binding(binding, durable=True, verifier=object())
    live = ensure_durable_binding(binding, durable=False, verifier=object())
    assert live.profile_id.startswith("live-")
    with pytest.raises(UnknownComponentProfileError):
        binding_for_resume(live)


def test_live_run_uses_factory_products() -> None:
    registry = default_component_registry()
    registration = resolve_selection(ComponentProfile(), registry=registry)
    context = ComponentFactoryContext(
        llm_client=object(), evidence_store=object(), graph_store=object()
    )
    components, binding = instantiate(registration, context)
    assert isinstance(components, RuntimeComponents)
    assert binding.profile_id == "builtin"
    assert binding_for_resume(binding, registry=registry) == binding


def test_every_run_gets_fresh_objects() -> None:
    registry = default_component_registry()
    registration = resolve_selection(None, registry=registry)
    context = ComponentFactoryContext(
        llm_client=object(), evidence_store=object(), graph_store=object()
    )
    first, _ = instantiate(registration, context)
    second, _ = instantiate(registration, context)
    assert first.agents is not second.agents
    assert first.result_mappers is not second.result_mappers






def test_factory_cannot_silently_replace_a_missing_component_with_builtin():
    registry = default_component_registry()
    profile = ComponentProfile(profile_id="incomplete", version="1")
    registry.register(
        profile,
        component_ids=dict(BUILTIN_COMPONENT_IDS),
        factory=lambda ctx: RuntimeComponents(object(), object(), None, object()),
    )
    with pytest.raises(ComponentBindingError):
        instantiate(registry.resolve(profile), ComponentFactoryContext(None, None, None))
