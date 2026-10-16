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
    if status is not None:
        assert f"Provider status: {status}" in markdown
    assert f"Provider retryable: {retryable}" in markdown
    assert sentinel not in restored.model_dump_json() + markdown
