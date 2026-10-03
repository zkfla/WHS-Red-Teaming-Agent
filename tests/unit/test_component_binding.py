"""Minimal checks for the A2 component binding contract."""

from __future__ import annotations

from typing import Any

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
    ComponentRegistry,
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


@pytest.mark.parametrize("override", [False, True])
async def test_custom_profile_assembly_with_identity_overrides(tmp_path, override) -> None:
    from redteam.app.bootstrap import build_runtime_deps
    from redteam.llm.fake import FakeLLMClient
    from redteam.orchestration.agents.providers import CandidateProviderRegistry
    from redteam.orchestration.agents.registry import AgentRegistry
    from redteam.orchestration.analysis import DeterministicAnalysisVerifier
    from redteam.orchestration.observations import ResultMapperRegistry
    from tests.integration._engine_llm_harness import make_engagement

    marker_verifier: Any = DeterministicAnalysisVerifier()
    marker_mappers: Any = ResultMapperRegistry()
    factory_agents = AgentRegistry()
    factory_providers = CandidateProviderRegistry()
    custom_ids = {
        "agents": "custom-agents-v1",
        "candidate_providers": "custom-providers-v1",
        "verifier": "custom-verifier-v1",
        "result_mappers": "custom-mappers-v1",
    }

    def custom_factory(context):
        assert context.llm_client is fake_llm
        return RuntimeComponents(
            agents=factory_agents,
            candidate_providers=factory_providers,
            verifier=marker_verifier,
            result_mappers=marker_mappers,
        )

    registry = ComponentRegistry()
    profile = ComponentProfile(profile_id="custom", version="1")
    registry.register(profile, component_ids=dict(custom_ids), factory=custom_factory)

    fake_llm = FakeLLMClient()
    deps = await build_runtime_deps(
        make_engagement(engagement_id="eng-custom-profile"),
        evidence_dir=tmp_path / "evidence",
        audit_dir=tmp_path / "audit",
        approval_dir=tmp_path / "approvals",
        llm=fake_llm,
        component_profile=profile,
        component_registry=registry,
        verifier=marker_verifier if override else None,
        result_mappers=marker_mappers if override else None,
    )
    assert deps.verifier is marker_verifier
    assert deps.result_mappers is marker_mappers
    assert deps.agents is factory_agents
    assert deps.candidate_providers is factory_providers
    assert deps.component_binding is not None
    if override:
        assert deps.component_binding.profile_id.startswith("live-")
        with pytest.raises(UnknownComponentProfileError):
            binding_for_resume(deps.component_binding, registry=registry)
    else:
        assert deps.component_binding.profile_id == "custom"
        assert deps.component_binding.component_ids == custom_ids
        resumed = binding_for_resume(deps.component_binding, registry=registry)
        assert resumed == deps.component_binding


async def test_durable_override_is_rejected_before_factory_or_transport(tmp_path, monkeypatch):
    from redteam.app.bootstrap import build_application
    from redteam.core.config import load_app_config
    from redteam.llm.fake import FakeLLMClient
    from redteam.orchestration.settings import OrchestrationSettings

    registry = default_component_registry()
    selection = ComponentProfile(profile_id="registered", version="1")

    def forbidden(*args, **kwargs):
        pytest.fail("invalid durable overrides must be rejected before construction")

    registry.register(selection, component_ids=dict(BUILTIN_COMPONENT_IDS), factory=forbidden)
    monkeypatch.setattr("redteam.app.bootstrap._build_default_capability", forbidden)
    config = load_app_config("examples/app.yaml")
    with pytest.raises(NonRebuildableComponentsError):
        await build_application(
            config,
            llm=FakeLLMClient(),
            verifier=object(),
            component_profile=selection,
            component_registry=registry,
            orchestration_settings=OrchestrationSettings(
                checkpointing=True,
                checkpoint_backend="sqlite",
                checkpoint_path=str(tmp_path / "checkpoints.sqlite3"),
            ),
            evidence_dir=tmp_path / "evidence",
        )


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
