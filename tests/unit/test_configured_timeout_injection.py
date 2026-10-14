"""Configured decision timeouts reach every targeted client construction path."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from redteam.app.runtime_components import (
    ComponentFactoryContext,
    default_component_registry,
    instantiate,
    resolve_selection,
)
from redteam.attack_graph.stores.memory import InMemoryAttackGraphStore
from redteam.llm.fake import FakeLLMClient
from redteam.orchestration.code_artifacts import prepare
from redteam.orchestration.cognition import DecisionBudget
from redteam.orchestration.local_tools import LocalToolContext
from redteam.orchestration.settings import OrchestrationSettings
from tests.fakes.evidence_store import FakeEvidenceStore


def configured_settings() -> OrchestrationSettings:
    return OrchestrationSettings(
        decision_timeout_seconds=777.0,
        llm_attempt_timeout_seconds=42.0,
    )


def test_builtin_factory_injects_configured_timeouts_and_legacy_context_defaults() -> None:
    registration = resolve_selection(None, registry=default_component_registry())
    llm = FakeLLMClient()
    evidence = FakeEvidenceStore()
    graph = InMemoryAttackGraphStore()
    settings = configured_settings()

    components, _ = instantiate(
        registration,
        ComponentFactoryContext(llm, evidence, graph, settings=settings),
    )

    assert components.verifier._decider._decision_timeout_seconds == 777.0
    assert components.verifier._decider._llm_attempt_timeout_seconds == 42.0
    assert components.verifier._decider._decision_timeout_seconds != 900.0
    assert components.verifier._decider._llm_attempt_timeout_seconds != 180.0

    legacy_components, _ = instantiate(
        registration,
        ComponentFactoryContext(FakeLLMClient(), FakeEvidenceStore(), InMemoryAttackGraphStore()),
    )
    assert legacy_components.verifier._decider._decision_timeout_seconds == 900.0
    assert legacy_components.verifier._decider._llm_attempt_timeout_seconds == 180.0


@pytest.mark.asyncio
async def test_code_artifact_prepare_forwards_configured_timeouts(monkeypatch) -> None:
    import redteam.orchestration.code_artifacts as code_artifacts

    settings = configured_settings()
    role_writer = SimpleNamespace(role_id="writer")
    role_reviewer = SimpleNamespace(role_id="reviewer")
    deps = SimpleNamespace(
        llm_client=FakeLLMClient(),
        settings=settings,
        agent_prompts=SimpleNamespace(
            binding=lambda stage: role_writer if stage == "code_generation" else role_reviewer,
        ),
    )
    context = LocalToolContext(
        deps=deps,
        runtime=SimpleNamespace(),
        purpose="timeout injection regression",
        budget=DecisionBudget(1),
    )

    async def capture_context(_context, _refs):
        return {}, {}, object()

    async def capture_windows(_context, _sources):
        return []

    captured: dict[str, object] = {}

    class StopAfterConstruction(Exception):
        pass

    def capture_client(llm, **kwargs):
        captured["llm"] = llm
        captured.update(kwargs)
        raise StopAfterConstruction

    monkeypatch.setattr(code_artifacts, "_capture", capture_context)
    monkeypatch.setattr(code_artifacts, "_windows", capture_windows)
    monkeypatch.setattr(code_artifacts, "StructuredDecisionClient", capture_client)

    result = await prepare(
        {"objective": "Review evidence", "source_evidence_ids": ["evidence"]},
        context,
    )

    assert result.ok is False
    assert captured["decision_timeout_seconds"] == 777.0
    assert captured["llm_attempt_timeout_seconds"] == 42.0
    assert captured["decision_timeout_seconds"] != 900.0
    assert captured["llm_attempt_timeout_seconds"] != 180.0
