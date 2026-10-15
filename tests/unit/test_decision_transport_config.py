"""Phase-1 config declaration for decision_transport_max_attempts (TASK-01)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from redteam.app.runtime_components import (
    ComponentFactoryContext,
    default_component_registry,
    instantiate,
    resolve_selection,
)
from redteam.attack_graph.stores.memory import InMemoryAttackGraphStore
from redteam.core.config import OrchestrationConfig
from redteam.core.ids import EngagementId, RunId
from redteam.event_bus import InMemoryEventBus
from redteam.llm.fake import FakeLLMClient
from redteam.orchestration.analysis import StructuredLLMAnalysisVerifier
from redteam.orchestration.analysis_budget import verification_budget
from redteam.orchestration.central_state import CentralStateStore
from redteam.orchestration.cognition import DecisionBudget
from redteam.orchestration.context import RuntimeDeps
from redteam.orchestration.engine import InvestigationEngine
from redteam.orchestration.settings import OrchestrationSettings
from redteam.orchestration.specialist_planner import SpecialistPlanner
from redteam.orchestration.state import GraphState, RuntimeState
from tests.fakes.control_plane import FakeControlPlane
from tests.fakes.evidence_store import FakeEvidenceStore
from tests.fakes.tool_gateway import DummyToolGateway


def test_transport_cap_default_is_five() -> None:
    assert OrchestrationConfig().decision_transport_max_attempts == 5
    assert OrchestrationSettings().decision_transport_max_attempts == 5
    assert OrchestrationSettings.from_config().decision_transport_max_attempts == 5


@pytest.mark.parametrize("value", [0, 1, 2, 5])
def test_transport_cap_bounds_accept(value: int) -> None:
    validated = {"decision_transport_max_attempts": value}
    assert OrchestrationConfig.model_validate(validated).decision_transport_max_attempts == value
    assert OrchestrationSettings.model_validate(validated).decision_transport_max_attempts == value


@pytest.mark.parametrize("value", [-1, 6, 100])
def test_transport_cap_bounds_reject(value: int) -> None:
    with pytest.raises(ValidationError):
        OrchestrationConfig.model_validate({"decision_transport_max_attempts": value})
    with pytest.raises(ValidationError):
        OrchestrationSettings.model_validate({"decision_transport_max_attempts": value})


def test_transport_cap_rejects_non_integer() -> None:
    with pytest.raises(ValidationError):
        OrchestrationConfig.model_validate({"decision_transport_max_attempts": "many"})


def test_env_mapping_reaches_config() -> None:
    config = OrchestrationConfig.from_environment({"REDTEAM_DECISION_TRANSPORT_MAX_ATTEMPTS": "2"})
    assert config.decision_transport_max_attempts == 2
    assert OrchestrationConfig.from_environment({}).decision_transport_max_attempts == 5


def test_env_mapping_rejects_out_of_bounds() -> None:
    with pytest.raises(ValidationError):
        OrchestrationConfig.from_environment({"REDTEAM_DECISION_TRANSPORT_MAX_ATTEMPTS": "6"})


def test_from_config_bridge_carries_value() -> None:
    config = OrchestrationConfig.model_validate({"decision_transport_max_attempts": 2})
    assert OrchestrationSettings.from_config(config).decision_transport_max_attempts == 2


def test_value_is_in_settings_identity_payloads() -> None:
    settings = OrchestrationSettings.model_validate({"decision_transport_max_attempts": 2})
    assert settings.central_payload()["decision_transport_max_attempts"] == 2
    assert settings.durable_payload()["decision_transport_max_attempts"] == 2
    assert OrchestrationSettings().central_payload()["decision_transport_max_attempts"] == 5


def test_existing_totals_unchanged() -> None:
    settings = OrchestrationSettings()
    assert settings.decision_normal_limit == 12
    assert settings.decision_max_attempts == 6


def _engine_deps(settings):
    return SimpleNamespace(llm_client=FakeLLMClient(), settings=settings, planning_provider=None)


def test_engine_default_path_lands_five_in_client_and_chain_budget() -> None:
    engine = InvestigationEngine(_engine_deps(OrchestrationSettings()))
    assert engine.client._transport_limit == 5
    budget = engine._decision_budget()
    assert budget.transport_limit == 5
    assert budget.max_attempts == 18
    assert engine.client._max_attempts == 6


def test_engine_non_default_value_lands() -> None:
    settings = OrchestrationSettings(decision_transport_max_attempts=2)
    engine = InvestigationEngine(_engine_deps(settings))
    assert engine.client._transport_limit == 2
    assert engine._decision_budget().transport_limit == 2
    assert engine._decision_budget().max_attempts == 18


def test_engine_zero_disables_transport_retries() -> None:
    settings = OrchestrationSettings(decision_transport_max_attempts=0)
    budget = InvestigationEngine(_engine_deps(settings))._decision_budget()
    assert budget.transport_limit == 0
    assert budget.permits(transport=True) is False
    assert budget.transport_used == 0


def _planner_deps(settings):
    policy = SimpleNamespace(probe_limits=None)
    return SimpleNamespace(llm_client=FakeLLMClient(), settings=settings, specialist_policy=policy)


def test_planner_default_and_non_default_land() -> None:
    defaulted = SpecialistPlanner(_planner_deps(OrchestrationSettings()))
    assert defaulted.client._transport_limit == 5
    tuned_settings = OrchestrationSettings(decision_transport_max_attempts=2)
    tuned = SpecialistPlanner(_planner_deps(tuned_settings))
    assert tuned.client._transport_limit == 2


def _runtime_deps(settings: OrchestrationSettings, tmp_path: Path) -> RuntimeDeps:
    return RuntimeDeps(
        control_plane=FakeControlPlane(),
        tool_gateway=DummyToolGateway(),
        event_bus=InMemoryEventBus(),
        graph_store=InMemoryAttackGraphStore(),
        evidence_store=FakeEvidenceStore(),
        llm_client=FakeLLMClient(),
        central_state=CentralStateStore(
            tmp_path / "state.db",
            run_id="run-transport-cap",
            engagement_id="engagement-transport-cap",
        ),
        settings=settings,
    )


def _graph_state() -> GraphState:
    runtime = RuntimeState(
        run_id=RunId("run-transport-cap"),
        engagement_id=EngagementId("engagement-transport-cap"),
    )
    return {"plan_step": None, "runtime": runtime}


def test_verification_budget_carries_configured_cap(tmp_path: Path) -> None:
    state = _graph_state()
    default_settings = OrchestrationSettings()
    tuned_settings = OrchestrationSettings(decision_transport_max_attempts=2)
    disabled_settings = OrchestrationSettings(decision_transport_max_attempts=0)
    defaulted = verification_budget(state, deps=_runtime_deps(default_settings, tmp_path))
    assert defaulted.transport_limit == 5
    tuned = verification_budget(state, deps=_runtime_deps(tuned_settings, tmp_path))
    assert tuned.transport_limit == 2
    disabled = verification_budget(state, deps=_runtime_deps(disabled_settings, tmp_path))
    assert disabled.transport_limit == 0
    assert disabled.permits(transport=True) is False


def test_factory_default_non_default_and_legacy_land_in_verifier() -> None:
    registration = resolve_selection(None, registry=default_component_registry())
    default_settings = OrchestrationSettings()
    tuned_settings = OrchestrationSettings(decision_transport_max_attempts=2)
    defaulted, _ = instantiate(
        registration,
        ComponentFactoryContext(
            FakeLLMClient(), FakeEvidenceStore(), InMemoryAttackGraphStore(),
            settings=default_settings,
        ),
    )
    assert defaulted.verifier._decider._transport_limit == 5
    tuned, _ = instantiate(
        registration,
        ComponentFactoryContext(
            FakeLLMClient(), FakeEvidenceStore(), InMemoryAttackGraphStore(),
            settings=tuned_settings,
        ),
    )
    assert tuned.verifier._decider._transport_limit == 2
    legacy, _ = instantiate(
        registration,
        ComponentFactoryContext(
            FakeLLMClient(), FakeEvidenceStore(), InMemoryAttackGraphStore(),
        ),
    )
    assert legacy.verifier._decider._transport_limit == 5


def test_verifier_defaults_keep_client_default() -> None:
    verifier = StructuredLLMAnalysisVerifier(FakeLLMClient(), FakeEvidenceStore(), None)
    assert verifier._decider._transport_limit == 5
    tuned = StructuredLLMAnalysisVerifier(
        FakeLLMClient(), FakeEvidenceStore(), None, transport_limit=2,
    )
    assert tuned._decider._transport_limit == 2
    disabled = StructuredLLMAnalysisVerifier(
        FakeLLMClient(), FakeEvidenceStore(), None, transport_limit=0,
    )
    assert disabled._decider._transport_limit == 0


def test_context_fallback_verifier_carries_cap(tmp_path: Path) -> None:
    deps = _runtime_deps(OrchestrationSettings(decision_transport_max_attempts=2), tmp_path)
    verifier = deps.verifier
    assert isinstance(verifier, StructuredLLMAnalysisVerifier)
    assert verifier._decider._transport_limit == 2


@pytest.mark.asyncio
async def test_code_artifact_prepare_forwards_transport_cap(monkeypatch) -> None:
    import redteam.orchestration.code_artifacts as code_artifacts
    from redteam.orchestration.local_tools import LocalToolContext
    captured: dict = {}
    class StopAfterConstruction(Exception):
        pass
    def capture_client(llm, **kwargs):
        captured.update(kwargs)
        raise StopAfterConstruction
    async def capture_context(_context, _refs):
        return {}, {}, object()
    async def capture_windows(_context, _sources):
        return []
    monkeypatch.setattr(code_artifacts, "_capture", capture_context)
    monkeypatch.setattr(code_artifacts, "_windows", capture_windows)
    monkeypatch.setattr(code_artifacts, "StructuredDecisionClient", capture_client)
    role_writer = SimpleNamespace(role_id="writer")
    role_reviewer = SimpleNamespace(role_id="reviewer")
    def binding(stage):
        return role_writer if stage == "code_generation" else role_reviewer
    deps = SimpleNamespace(
        llm_client=FakeLLMClient(),
        settings=OrchestrationSettings(),
        agent_prompts=SimpleNamespace(binding=binding),
    )
    context = LocalToolContext(
        deps=deps,
        runtime=SimpleNamespace(),
        purpose="transport cap",
        budget=DecisionBudget(1),
    )
    result = await code_artifacts.prepare(
        {"objective": "Review evidence", "source_evidence_ids": ["evidence"]},
        context,
    )
    assert result.ok is False
    assert captured["transport_limit"] == 5
    captured.clear()
    tuned_deps = SimpleNamespace(
        llm_client=FakeLLMClient(),
        settings=OrchestrationSettings(decision_transport_max_attempts=2),
        agent_prompts=SimpleNamespace(binding=binding),
    )
    tuned_context = LocalToolContext(
        deps=tuned_deps,
        runtime=SimpleNamespace(),
        purpose="transport cap",
        budget=DecisionBudget(1),
    )
    tuned_result = await code_artifacts.prepare(
        {"objective": "Review evidence", "source_evidence_ids": ["evidence"]},
        tuned_context,
    )
    assert tuned_result.ok is False
    assert captured["transport_limit"] == 2
