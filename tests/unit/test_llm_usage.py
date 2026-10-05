"""Provider usage survives format repair and remains local to each request."""

from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from redteam.llm.litellm import LiteLLMClient
from redteam.orchestration.cognition import (
    DecisionBudget,
    DecisionFailure,
    ReasoningHalted,
    StructuredDecisionClient,
)

SCHEMA = {
    "type": "object",
    "properties": {"answer": {"type": "string"}},
    "required": ["answer"],
    "additionalProperties": False,
}


def client(monkeypatch, responses):
    async def completion(**kwargs):
        assert kwargs["num_retries"] == 0
        text, tokens = responses.pop(0)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=text))],
            usage=SimpleNamespace(total_tokens=tokens) if tokens is not None else None,
        )

    monkeypatch.setitem(
        sys.modules,
        "litellm",
        SimpleNamespace(
            get_llm_provider=lambda model: (model, "openai", None, None),
            acompletion=completion,
        ),
    )
    return LiteLLMClient("unit-model")


@pytest.mark.asyncio
async def test_bad_json_repair_retains_reported_tokens(monkeypatch):
    provider = client(monkeypatch, [("not json", 11), ('{"answer":"done"}', 13)])
    budget = DecisionBudget(3)

    async def guard():
        assert not budget.token_halt_required(30)

    result = await StructuredDecisionClient(provider).ask(
        stage="usage-repair",
        payload={},
        response_schema=SCHEMA,
        budget=budget,
        before_call=guard,
    )
    assert result == {"answer": "done"}
    assert budget.used == 2
    assert budget.tokens_used == 24
    assert budget.tokens_fully_known


@pytest.mark.asyncio
async def test_missing_usage_does_not_reuse_previous_request_cost(monkeypatch):
    provider = client(monkeypatch, [('{"answer":"one"}', 7), ('{"answer":"two"}', None)])
    decider = StructuredDecisionClient(provider)
    first, second = DecisionBudget(1), DecisionBudget(1)
    for budget in (first, second):
        await decider.ask(stage="usage-local", payload={}, response_schema=SCHEMA, budget=budget)
    assert first.tokens_used == 7 and first.tokens_fully_known
    assert second.unknown_usage_count == 1
    assert second.token_halt_required(20)


@pytest.mark.parametrize("expire_before_retry", [False, True])
async def test_analysis_requests_share_actual_delegation_cost_and_fresh_time_guard(
    expire_before_retry,
):
    import dataclasses
    from datetime import datetime, timezone

    from redteam.core.contracts.graph import Hypothesis
    from redteam.core.enums import EvidenceSourceType, Verdict
    from redteam.core.ports.llm import StructuredCompletion
    from redteam.orchestration.analysis import StructuredLLMAnalysisVerifier
    from redteam.orchestration.delegation import DelegationRequest
    from redteam.orchestration.delegation_decision import open_delegation
    from redteam.orchestration.nodes.verify import verify_node
    from redteam.orchestration.state import RuntimeState, initial_graph_state
    from tests.integration import _engine_llm_harness as h
    from tests.unit import test_semantic_completion_receipts as receipts

    store, _, service, original_deps = receipts._harness(receipts._success_llm())
    delegated = open_delegation(
        DelegationRequest(
            path_step_id=receipts.STEP_ID,
            agent_id=str(receipts.AGENT),
            objective="review the actual response",
        ),
        path_id=receipts.PATH_ID,
        path_version=1,
        decision_limit=5,
    )
    runtime = RuntimeState(
        run_id=receipts.RUN,
        engagement_id=receipts.ENGAGEMENT,
        delegations={delegated.delegation_id: delegated},
        started_at=datetime.now(timezone.utc),
    )

    class Reviewer:
        calls = 0

        async def complete_structured_with_usage(self, **kwargs):
            self.calls += 1
            assert kwargs["num_retries"] == 0
            if self.calls == 1:
                if expire_before_retry:
                    runtime.started_at = datetime(2000, 1, 1, tzinfo=timezone.utc)
                return StructuredCompletion(value="invalid json", total_tokens=5)
            return StructuredCompletion(value=receipts._success_llm(), total_tokens=7)

    reviewer = Reviewer()
    engagement = h.make_engagement(receipts.HOST, engagement_id=str(receipts.ENGAGEMENT))
    engagement = engagement.model_copy(
        update={
            "execution_budget": engagement.execution_budget.model_copy(
                update={"max_duration_seconds": 300}
            )
        }
    )
    deps = dataclasses.replace(
        original_deps,
        engagement=engagement,
        verifier=StructuredLLMAnalysisVerifier(reviewer, store, service),
    )
    proposal, result = receipts._proposal(), receipts._result()
    await service.record_hypothesis(
        Hypothesis(
            hypothesis_id=str(proposal.hypothesis_id),
            description="target returns 200",
        )
    )
    state = initial_graph_state(runtime)
    state.update(
        action_proposal=proposal,
        tool_result=result,
        plan_step=receipts._plan_step(violation=False).model_copy(
            update={"delegation_id": delegated.delegation_id}
        ),
    )
    output = await verify_node(state, deps=deps)
    spent = output["runtime"].delegations[delegated.delegation_id]
    assert reviewer.calls == (1 if expire_before_retry else 2)
    assert spent.decision_attempts_used == reviewer.calls
    assert spent.tokens_used == (5 if expire_before_retry else 12)
    assert output["runtime"].last_verdict is (
        Verdict.UNKNOWN if expire_before_retry else Verdict.PASS
    )
    originals = [
        record for record in store.records if record.source_type is EvidenceSourceType.TOOL
    ]
    assert len(originals) == 1
    assert originals[0].metadata["tool_result"]["output"] == result.output


async def test_normal_decisions_and_format_repairs_share_usage_without_reset(monkeypatch):
    provider = client(monkeypatch, [
        ("invalid json", 5), ('{"answer":"first"}', 7), ('{"answer":"second"}', 11),
    ])
    budget = DecisionBudget.reasoning_chain(2, 2)
    decider = StructuredDecisionClient(provider)
    for stage in ("draft", "reconsider"):
        await decider.ask(stage=stage, payload={}, response_schema=SCHEMA, budget=budget)
    with pytest.raises(DecisionFailure) as caught:
        await decider.ask(stage="review", payload={}, response_schema=SCHEMA, budget=budget)
    assert caught.value.diagnostic.code == "decision_normal_budget_exhausted"
    assert (budget.used, budget.normal_used, budget.repair_used) == (3, 2, 1)
    assert budget.tokens_used == 23
    assert budget.unknown_usage_count == 0


async def test_repair_exhaustion_does_not_spend_the_next_normal_decision(monkeypatch):
    provider = client(monkeypatch, [
        ("invalid json", 3), ("still invalid", 5), ('{"answer":"new plan"}', 7),
    ])
    budget = DecisionBudget.reasoning_chain(3, 1)
    decider = StructuredDecisionClient(provider)
    with pytest.raises(DecisionFailure) as caught:
        await decider.ask(stage="draft", payload={}, response_schema=SCHEMA, budget=budget)
    assert caught.value.diagnostic.code == "decision_repair_budget_exhausted"
    assert await decider.ask(
        stage="new_plan", payload={}, response_schema=SCHEMA, budget=budget,
    ) == {"answer": "new plan"}
    assert (budget.used, budget.normal_used, budget.repair_used) == (3, 2, 1)
    assert budget.tokens_used == 15


@pytest.mark.parametrize("termination", [None, "budget_exhausted"])
async def test_guard_denial_before_repair_counts_only_the_request_already_sent(
    monkeypatch, termination,
):
    provider = client(monkeypatch, [("invalid json", 13)])
    budget = DecisionBudget.reasoning_chain(2, 2)
    guards = 0

    async def guard():
        nonlocal guards
        guards += 1
        if guards == 2:
            raise ReasoningHalted("deadline_reached", termination_reason=termination)

    with pytest.raises(DecisionFailure) as caught:
        await StructuredDecisionClient(provider).ask(
            stage="deadline", payload={}, response_schema=SCHEMA,
            budget=budget, before_call=guard,
        )
    assert caught.value.diagnostic.code == "reasoning_guard_blocked"
    assert caught.value.diagnostic.termination_reason == termination
    assert caught.value.diagnostic.last_cause.code == "invalid_json"
    assert (budget.used, budget.normal_used, budget.repair_used) == (1, 1, 0)
    assert budget.tokens_used == 13 and budget.unknown_usage_count == 0


async def test_worker_total_limit_narrows_combined_normal_and_repair_budget(monkeypatch):
    provider = client(monkeypatch, [("invalid json", 3), ('{"answer":"done"}', 5)])
    budget = DecisionBudget.reasoning_chain(8, 3, total_limit=2)
    decider = StructuredDecisionClient(provider)
    await decider.ask(stage="worker", payload={}, response_schema=SCHEMA, budget=budget)
    with pytest.raises(DecisionFailure) as caught:
        await decider.ask(stage="worker_next", payload={}, response_schema=SCHEMA, budget=budget)
    assert caught.value.diagnostic.code == "decision_budget_exhausted"
    assert budget.used == 2 and budget.tokens_used == 8


async def test_normal_exhaustion_takes_graceful_continue_without_extra_request():
    """Normal-lane exhaustion with repair left stays on continue_planning."""
    from redteam.attack_graph.observation import GraphObservation
    from redteam.orchestration.engine import InvestigationEngine
    from redteam.orchestration.paths import CompletionCriterion, PathPlan, PathStep
    from redteam.orchestration.settings import OrchestrationSettings
    from tests.integration import _engine_llm_harness as h

    llm = h.ScriptedLLM()
    seed = await h.seed_host("run-t8-bound")
    world = h.make_world(llm, graph_store=seed.store)
    world.deps.settings = OrchestrationSettings(
        decision_normal_limit=1, decision_max_attempts=1
    )
    step = PathStep(
        step_id="step-1",
        objective="inventory the host",
        agent_id=h.RECON_AGENT,
        target_node_ids=[seed.node_id],
        allowed_operations=[h.CANONICAL_OP],
        completion_criteria=[
            CompletionCriterion(kind="evidence", description="x", min_evidence=1),
        ],
    )
    await world.paths.register(PathPlan(
        path_id="path-t8-bound",
        run_id="run-t8-bound",
        version=1,
        status="active",
        reason="t8",
        goal="g",
        steps=[step],
    ))
    runtime = h.make_runtime("run-t8-bound", "eng-t8")
    engine = InvestigationEngine(world.deps)
    llm.add("next_action", h.turn_complete_step(["ev-t8-1"]))

    path, _, outcome = await engine.decide(runtime, GraphObservation())

    assert outcome == "continue_planning"
    assert path is not None
    assert engine.planning_diagnostic is not None
    assert engine.planning_diagnostic.code == "decision_normal_budget_exhausted"
    assert engine.planning_diagnostic.last_cause is None
    assert len(llm.calls) == 1


async def test_repair_drain_stays_on_the_error_track():
    """Repair-lane exhaustion still raises instead of continuing."""
    from redteam.attack_graph.observation import GraphObservation
    from redteam.orchestration.engine import InvestigationEngine
    from redteam.orchestration.paths import CompletionCriterion, PathPlan, PathStep
    from redteam.orchestration.settings import OrchestrationSettings
    from tests.integration import _engine_llm_harness as h

    llm = h.ScriptedLLM()
    seed = await h.seed_host("run-t8-repair")
    world = h.make_world(llm, graph_store=seed.store)
    world.deps.settings = OrchestrationSettings(
        decision_normal_limit=2, decision_max_attempts=1
    )
    step = PathStep(
        step_id="step-1",
        objective="inventory the host",
        agent_id=h.RECON_AGENT,
        target_node_ids=[seed.node_id],
        allowed_operations=[h.CANONICAL_OP],
        completion_criteria=[
            CompletionCriterion(kind="evidence", description="x", min_evidence=1),
        ],
    )
    await world.paths.register(PathPlan(
        path_id="path-t8-repair",
        run_id="run-t8-repair",
        version=1,
        status="active",
        reason="t8",
        goal="g",
        steps=[step],
    ))
    runtime = h.make_runtime("run-t8-repair", "eng-t8")
    engine = InvestigationEngine(world.deps)
    llm.add("next_action", {"bogus": True})
    llm.add("next_action", {"bogus": True})

    with pytest.raises(DecisionFailure) as caught:
        await engine.decide(runtime, GraphObservation())

    assert caught.value.diagnostic.code == "decision_repair_budget_exhausted"
    assert caught.value.diagnostic.termination_reason is None
    assert len(llm.calls) == 2


@pytest.mark.parametrize("mode", ["revise_path", "switch_path"])
async def test_revised_path_review_normal_exhaustion_defers_without_admitting_candidate(mode):
    from redteam.attack_graph.observation import GraphObservation
    from redteam.orchestration.engine import InvestigationEngine
    from redteam.orchestration.paths import PathPlan
    from redteam.orchestration.settings import OrchestrationSettings
    from tests.integration import _engine_llm_harness as h

    run_id = "run-review-bound"
    llm = h.ScriptedLLM()
    seed = await h.seed_host(run_id)
    world = h.make_world(llm, graph_store=seed.store)
    world.deps.settings = OrchestrationSettings(decision_normal_limit=2)
    active = PathPlan(
        **h.draft_path(seed.node_id), path_id="path-review-bound", run_id=run_id,
        version=1, status="active", goal="g",
    )
    active.steps[0].status = "completed"
    active.steps[0].evidence_ids = ["ev-prior-progress"]
    active = await world.paths.register(active)
    proposed = h.draft_path(seed.node_id)
    proposed["steps"][0]["objective"] = "collect additional evidence"
    llm.add("next_action", {"mode": mode, "reason": "change path", "path": proposed})
    llm.add("path_review", {"decision": "revise", "reason": "review replacement", "path": proposed})
    runtime = h.make_runtime(run_id, "eng-review-bound")
    engine = InvestigationEngine(world.deps)

    path, actions, outcome = await engine.decide(runtime, GraphObservation())

    assert outcome == "continue_planning" and actions == []
    assert path == active
    assert await world.paths.get_active(run_id) == active
    assert engine.planning_diagnostic is not None
    assert engine.planning_diagnostic.code == "decision_normal_budget_exhausted"
    assert engine.planning_diagnostic.stage == "path_review"
    assert engine.planning_diagnostic.normal_used == 2
    assert engine.planning_diagnostic.repair_used == 0
    assert [call["route"] for call in llm.calls] == ["next_action", "path_review"]

    llm.add("next_action", {"mode": "hold", "reason": "await additional evidence"})
    next_engine = InvestigationEngine(world.deps)
    path, actions, outcome = await next_engine.decide(runtime, GraphObservation())
    assert path == active and actions == [] and outcome == "await additional evidence"
    assert len(llm.calls) == 3


@pytest.mark.parametrize(
    "status,code,retry_after,expected_kind,retryable",
    [(401, "invalid_api_key", None, "auth", False),
     (400, "bad_request", None, "invalid_request", False),
     (429, "rate_limit", "2.5", "rate_limited", True),
     (503, "unavailable", None, "server", True),
     (None, None, None, "unknown", True)],
)
async def test_provider_failure_reaches_persisted_report_without_remote_material(
    monkeypatch, tmp_path, status, code, retry_after, expected_kind, retryable,
):
    from redteam.app.cli.commands.report import _render_markdown
    from redteam.app.report_store import load_terminal_report, save_terminal_report
    from redteam.attack_graph.service import AttackGraphService
    from redteam.attack_graph.stores.memory import InMemoryAttackGraphStore
    from redteam.core.enums import AgentPhase
    from redteam.orchestration.reporting import ReportingAgent
    from redteam.orchestration.state import RuntimeState
    from redteam.orchestration.work.memory import InMemoryWorkStore
    from tests.fakes.evidence_store import FakeEvidenceStore

    sentinel = "opaque-remote-body-not-for-report"

    class RemoteError(Exception):
        pass

    error = RemoteError(sentinel)
    error.status_code = status
    error.code = code
    error.body = {"message": sentinel}
    error.headers = {"Authorization": f"Bearer {sentinel}", "retry-after": retry_after}
    calls = 0

    async def completion(**kwargs):
        nonlocal calls
        calls += 1
        assert kwargs["num_retries"] == 0
        raise error

    monkeypatch.setitem(sys.modules, "litellm", SimpleNamespace(
        get_llm_provider=lambda model: (model, "openai", None, None),
        acompletion=completion,
    ))
    budget = DecisionBudget.reasoning_chain(3, 1)
    with pytest.raises(DecisionFailure) as caught:
        await StructuredDecisionClient(LiteLLMClient("unit-model")).ask(
            stage="provider", payload={}, response_schema=SCHEMA, budget=budget,
        )
    failure = caught.value.diagnostic
    cause = failure.last_cause if retryable else failure
    assert cause.code == "llm_provider_" + expected_kind
    assert cause.provider_status == status and cause.provider_code == (code or "unknown")
    assert cause.retryable is retryable
    assert cause.retry_after == (float(retry_after) if retry_after else None)
    assert calls == (2 if retryable else 1)
    assert budget.used == calls and budget.unknown_usage_count == calls
    assert budget.tokens_used == 0
    runtime = RuntimeState(
        run_id="run-provider", engagement_id="eng", phase=AgentPhase.FAILED,
        termination_reason="planning_error", planning_diagnostic=failure,
        model_requests_used=budget.used, model_tokens_used=budget.tokens_used,
        model_usage_unknown=budget.unknown_usage_count,
    )
    runtime = RuntimeState.model_validate_json(runtime.model_dump_json())
    report = await ReportingAgent().project(
        runtime, graph=AttackGraphService(InMemoryAttackGraphStore()),
        evidence_store=FakeEvidenceStore(), work_store=InMemoryWorkStore(),
    )
    save_terminal_report(report, reports_dir=tmp_path)
    restored = load_terminal_report(runtime.run_id, reports_dir=tmp_path)
    assert restored is not None and restored.planning_diagnostic == failure
    assert restored.model_requests_used == calls and restored.model_usage_unknown == calls
    assert restored.run_outcome == "failed" and not restored.ready_for_release
    markdown = _render_markdown(restored)
    assert cause.code in markdown
    assert sentinel not in restored.model_dump_json() + markdown
