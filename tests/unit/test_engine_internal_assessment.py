"""W5 engine wiring: execution checks read fresh internal material only."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from redteam.attack_graph.observation import build_observation
from redteam.attack_graph.predicates import Confidence, Provenance
from redteam.attack_graph.service import AttackGraphService
from redteam.core.contracts.target import HostTarget
from redteam.core.enums import GraphNodeType
from redteam.core.ids import AgentId
from redteam.orchestration import engine as engine_module
from redteam.orchestration.agents.base import AgentSpec, PlanStep
from redteam.orchestration.agents.definition import AgentDefinition, ContextProfile
from redteam.orchestration.agents.providers import (
    CandidateBatch,
    CandidateProviderRegistry,
)
from redteam.orchestration.agents.registry import AgentRegistry
from redteam.orchestration.diagnostics import diagnostic_for_exception
from redteam.orchestration.engine import (
    DecisionRuleError,
    InvestigationEngine,
    _collect_check_assessment,
    _rejection_feedback,
    validate_current_action,
)
from redteam.orchestration.knowledge import project_knowledge
from redteam.orchestration.knowledge_validation import InternalAssessment
from redteam.orchestration.paths import CompletionCriterion, PathPlan, PathStep
from tests.fakes.bootstrap import build_test_application


def _runtime(run_id: str = "run-1"):
    return SimpleNamespace(
        run_id=run_id,
        engagement_id="eng-1",
        mission_id=None,
        active_task_id=None,
        current_goal="goal",
        step_count=0,
    )


def _deps():
    return build_test_application()


async def test_collect_rejects_unbound_material(monkeypatch):
    async def wrong_run(*args, **kwargs):
        return InternalAssessment(run_id="other-run")

    monkeypatch.setattr(engine_module, "collect_assessment", wrong_run)
    with pytest.raises(DecisionRuleError, match="not bound to this run"):
        await _collect_check_assessment(_deps(), _runtime(), service=object())

    async def wrong_shape(*args, **kwargs):
        return {"run_id": "run-1"}

    monkeypatch.setattr(engine_module, "collect_assessment", wrong_shape)
    with pytest.raises(DecisionRuleError, match="not bound to this run"):
        await _collect_check_assessment(_deps(), _runtime(), service=object())


async def test_execution_admission_blocked_on_lookup_failure(monkeypatch):
    calls = []

    async def failing_lookup(*args, **kwargs):
        calls.append(1)
        return InternalAssessment(run_id="run-1", graph_lookup_failed=True)

    monkeypatch.setattr(engine_module, "collect_assessment", failing_lookup)
    deps = _deps()
    step = SimpleNamespace(
        path_id="path-1",
        path_version=1,
        path_step_id="step-1",
        resolved_target=lambda: SimpleNamespace(host="h.local"),
        operation_key="web.http_request",
        arguments={},
        principal_ref=None,
        operator_intent_id=None,
        availability_impact=False,
    )
    current = SimpleNamespace(
        step_id="step-1",
        target_node_ids=["node-1"],
        allowed_operations=["web.http_request"],
        principal_refs=[],
        agent_id=None,
        objective="check",
    )
    path = SimpleNamespace(path_id="path-1", version=1, steps=[current])

    class Paths:
        async def get_active(self, run_id):
            return path

    deps = SimpleNamespace(
        graph_store=deps.graph_store,
        evidence_store=deps.evidence_store,
        path_store=Paths(),
        task_graph_store=deps.task_graph_store,
        engagement=None,
    )
    problems = await validate_current_action(deps, _runtime(), step)
    assert len(problems) == 1
    assert "lookup failed" in problems[0]
    assert calls == [1]


async def test_execution_uses_hidden_target_and_rechecks_its_removal():
    deps = _deps()
    runtime = _runtime()
    service = AttackGraphService(deps.graph_store)
    for index in range(101):
        await service.upsert_node(
            GraphNodeType.HOST,
            key=f"scope-{index:03}",
            attrs={"in_scope": True},
            provenance=Provenance(
                step=0,
                source="engagement:scope",
                run_id=runtime.run_id,
                confidence=Confidence.SERVER_DECLARED,
            ),
        )
    display = await project_knowledge(service, deps.evidence_store, runtime.run_id)
    assert "host:scope-100" not in {item["node_id"] for item in display["known_targets"]}
    path = await deps.path_store.register(
        PathPlan(
            path_id="path-101",
            run_id=runtime.run_id,
            version=1,
            status="active",
            reason="check target beyond display limit",
            goal="inspect scope",
            steps=[
                PathStep(
                    step_id="step-101",
                    objective="inspect last supplied host",
                    target_node_ids=["host:scope-100"],
                    allowed_operations=["recon.service_discovery"],
                    completion_criteria=[CompletionCriterion(kind="evidence", description="seen")],
                )
            ],
        )
    )
    step = PlanStep(
        step_id="step-101",
        path_id=path.path_id,
        path_version=path.version,
        path_step_id="step-101",
        capability="recon",
        operation="service_discovery",
        tool_id="recon.http_probe",
        target=HostTarget(host="scope-100"),
        reason="inspect supplied host",
    )
    assert await validate_current_action(deps, runtime, step) == []
    await deps.graph_store.remove_node("host:scope-100")
    assert "action target is not supplied or observed" in await validate_current_action(
        deps, runtime, step
    )


def _draft(operation="web.http_request", reason="r", targets=("n1",), prereqs=("p1",)):
    return SimpleNamespace(
        plan_step=SimpleNamespace(operation_key=operation, reason=reason),
        target_node_ids=list(targets),
        prerequisite_ids=list(prereqs),
    )


def _engine_with_provider(provider_id="test:fake", provider=None):
    registry = AgentRegistry()
    agent_id = AgentId("agent-1")
    registry.register_definition(
        AgentDefinition(
            agent_id=agent_id,
            role="role",
            name="name",
            capabilities=["web"],
            provider_id=provider_id,
            context_profile=ContextProfile(profile_id="profile"),
        )
    )
    registry.register(AgentSpec(agent_id=agent_id, name="name", capabilities=["web"]))
    providers = CandidateProviderRegistry(
        [(provider_id, provider)] if provider is not None else []
    )
    return InvestigationEngine(
        build_test_application(agents=registry, candidate_providers=providers)
    )


class _BatchProvider:
    def __init__(self, drafts):
        self.drafts = drafts
        self.seen = []

    async def propose_graph(self, spec, context):
        self.seen.append(context)
        return CandidateBatch(candidates=list(self.drafts))


class _ListProvider:
    def __init__(self, drafts):
        self.drafts = drafts

    async def propose_graph(self, spec, context):
        return list(self.drafts)


async def test_reference_paths_use_registry_provider_and_keep_order():
    provider = _BatchProvider([_draft(reason="first"), _draft(reason="second")])
    engine = _engine_with_provider(provider=provider)
    deps = engine.deps
    service = AttackGraphService(deps.graph_store)
    references = await engine.reference_paths(
        build_observation([], step=0), service, _runtime()
    )
    assert [item["purpose"] for item in references] == ["first", "second"]
    assert references[0]["operation"] == "web.http_request"
    assert references[0]["targets"] == ["n1"]
    assert references[0]["prerequisites"] == ["p1"]
    assert engine.last_reference_diagnostics == []
    assert not hasattr(engine_module, "PlaybookCandidateProvider")


async def test_reference_paths_normalize_list_and_report_missing():
    engine = _engine_with_provider(provider=_ListProvider([_draft()]))
    deps = engine.deps
    service = AttackGraphService(deps.graph_store)
    references = await engine.reference_paths(
        build_observation([], step=0), service, _runtime()
    )
    assert len(references) == 1

    missing = _engine_with_provider(provider_id="test:missing")
    service = AttackGraphService(missing.deps.graph_store)
    assert (
        await missing.reference_paths(build_observation([], step=0), service, _runtime())
        == []
    )
    codes = [item.code for item in missing.last_reference_diagnostics]
    assert "reference_unavailable" in codes


async def test_reference_paths_report_failed_provider():
    class _Boom:
        async def propose_graph(self, spec, context):
            raise RuntimeError("boom")

    engine = _engine_with_provider(provider=_Boom())
    service = AttackGraphService(engine.deps.graph_store)
    assert (
        await engine.reference_paths(build_observation([], step=0), service, _runtime())
        == []
    )
    assert [
        item.code for item in engine.last_reference_diagnostics
    ] == ["reference_unavailable"]


async def test_repair_references_uses_same_registry_path():
    provider = _BatchProvider([_draft(reason="only")])
    engine = _engine_with_provider(provider=provider)
    service = AttackGraphService(engine.deps.graph_store)
    intent = SimpleNamespace(
        target_node_id="n1", capability="web", operation="http_request"
    )
    repaired = await engine.repair_references(
        build_observation([], step=0), service, _runtime(), [intent]
    )
    assert repaired["omitted_count"] == 0
    assert [item["operation"] for item in repaired["items"]] == ["web.http_request"]


def test_rejection_feedback_codes_stay_distinct():
    assert _rejection_feedback("unknown_operation", "").startswith("unknown_operation")
    assert _rejection_feedback("missing_required_argument", "").startswith(
        "invalid_arguments"
    )
    assert _rejection_feedback("unresolved_target", "").startswith("unresolved_target")
    assert _rejection_feedback("target_mismatch", "").startswith("unresolved_target")
    assert _rejection_feedback("tool_outside_capability_namespace", "").startswith(
        "tool_outside_capability_namespace"
    )
    assert "weird-field" not in _rejection_feedback("unknown_operation", "weird-field")


def test_decision_rule_error_carries_code_location():
    error = DecisionRuleError(
        "action operation is not a supported action",
        code="unknown_operation",
        location="$.actions[0].operation",
    )
    assert error.diagnostic.code == "unknown_operation"
    assert error.diagnostic.location == "$.actions[0].operation"
    assert (
        diagnostic_for_exception(error, stage="action_compilation") is error.diagnostic
    )
    legacy = DecisionRuleError("plain summary")
    assert legacy.diagnostic.code == "decision_rule_violation"
