"""Provider fixtures only: no real model calls, costs or target traffic."""

from __future__ import annotations

import asyncio
import sys
from email.utils import formatdate
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from redteam.core.config import LLMConfig
from redteam.core.ports.llm import LLMProviderFailure, ProviderRetryPolicy
from redteam.llm import provider_backoff
from redteam.llm.litellm import LiteLLMClient, _normalize_provider_error, _retry_after_from_headers
from redteam.llm.provider_backoff import ProviderBackoff
from redteam.orchestration.cognition import (
    DecisionBudget,
    DecisionFailure,
    StructuredDecisionClient,
)
from tests.unit.test_llm_usage import SCHEMA


@pytest.fixture
def clock(monkeypatch):
    now = [0.0]

    async def advance(seconds):
        now[0] += seconds
        await asyncio.sleep(0)

    monkeypatch.setattr(provider_backoff, "time", SimpleNamespace(monotonic=lambda: now[0]))
    monkeypatch.setattr(provider_backoff, "asyncio", SimpleNamespace(sleep=advance))
    monkeypatch.setattr(provider_backoff.random, "uniform", lambda *_: 0.0)
    return now


def remote_error(code="slow_down", delay=None):
    error = RuntimeError("private-provider-body")
    error.status_code = 429
    error.code = "429"
    error.body = {"error": {"code": code, "message": "private-provider-body"}}
    error.headers = {"Retry-After": delay} if delay is not None else {}
    return error


def sdk_client(monkeypatch, replies, clock, *, policy=None):
    calls = []

    async def completion(**kwargs):
        calls.append((clock[0], kwargs))
        assert kwargs["num_retries"] == 0
        reply = replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=reply))],
            usage=SimpleNamespace(total_tokens=7),
        )

    monkeypatch.setitem(sys.modules, "litellm", SimpleNamespace(
        get_llm_provider=lambda model: (model, "openai", None, None),
        acompletion=completion,
    ))
    return LiteLLMClient("fixture-only", retry_policy=policy), calls


@pytest.mark.parametrize("code", [
    "insufficient_quota", "credit_balance_exhausted", "organization_spend_limit_exceeded",
    "project_spend_limit_exceeded", "organization_usage_limit_exceeded",
])
def test_quota_requires_operator_action_without_free_text(code):
    failure = _normalize_provider_error(remote_error(code, "1"))
    assert failure.kind == "quota" and not failure.retryable
    assert failure.provider_code == code
    assert "private-provider-body" not in str(failure) + str(failure.diagnostic_fields())


def test_quota_type_and_unknown_429_are_not_guessed():
    error = remote_error(None)
    error.body["error"]["type"] = "insufficient_quota"
    assert _normalize_provider_error(error).kind == "quota"
    error.body = {"message": "quota wording cannot be trusted for classification"}
    assert _normalize_provider_error(error).kind == "rate_limited"
    assert _normalize_provider_error(error).provider_code == "429"


@pytest.mark.parametrize("headers,expected", [
    ({"Retry-After": "9000"}, 9000.0), ({"retry-after-ms": "1500"}, 1.5),
    ({"Retry-After": "NaN"}, None), ({"Retry-After": "-1"}, None),
    ({"ratelimit-reset": "1900000000"}, None),
])
def test_delay_does_not_clip_floor_or_guess_reset_semantics(headers, expected):
    assert _retry_after_from_headers(headers) == expected
    if expected is not None:
        assert LLMProviderFailure(retry_after=expected).retry_after == expected


def test_http_date_delay(monkeypatch):
    from redteam.llm import litellm

    monkeypatch.setattr(litellm, "time", SimpleNamespace(time=lambda: 1000.0))
    assert _retry_after_from_headers({"Retry-After": formatdate(1010, usegmt=True)}) == 10


@pytest.mark.asyncio
async def test_rate_limit_then_format_repair_preserves_separate_budgets(monkeypatch, clock):
    llm, calls = sdk_client(monkeypatch, [
        remote_error(delay="2.5"), "bad JSON", '{"answer":"ok"}',
    ], clock)
    budget = DecisionBudget.reasoning_chain(2, 1)
    result = await StructuredDecisionClient(llm).ask(
        stage="arbitrary-role-stage", payload={}, response_schema=SCHEMA, budget=budget,
    )
    assert result == {"answer": "ok"}
    assert calls[1][0] >= 2.5
    assert "Previous attempt" not in calls[1][1]["messages"][-1]["content"]
    assert "Previous attempt" in calls[2][1]["messages"][-1]["content"]
    assert (budget.used, budget.transport_used, budget.normal_used, budget.repair_used) == (
        3, 1, 1, 1,
    )
    assert budget.unknown_usage_count == 1 and budget.tokens_used == 14


@pytest.mark.asyncio
async def test_long_delay_defers_before_consuming_another_request(monkeypatch, clock):
    llm, calls = sdk_client(monkeypatch, [remote_error(delay="9000")], clock)
    budget = DecisionBudget.reasoning_chain(3, 1)
    with pytest.raises(DecisionFailure) as caught:
        await StructuredDecisionClient(llm).ask(
            stage="any", payload={}, response_schema=SCHEMA, budget=budget,
        )
    diag = caught.value.diagnostic
    assert diag.termination_reason == "provider_unavailable" and diag.retry_after == 9000
    assert len(calls) == budget.used == budget.unknown_usage_count == 1
    assert budget.repair_used == 0 and clock[0] == 0


@pytest.mark.asyncio
async def test_quota_blocks_sibling_connection_without_request(monkeypatch, clock):
    llm, calls = sdk_client(monkeypatch, [remote_error("insufficient_quota")], clock)
    first, second = DecisionBudget(3), DecisionBudget(3)
    for budget in (first, second):
        with pytest.raises(DecisionFailure) as caught:
            await StructuredDecisionClient(llm).ask(
                stage="different-role", payload={}, response_schema=SCHEMA, budget=budget,
            )
        assert caught.value.diagnostic.code == "llm_provider_quota"
    assert first.used == 1 and second.used == 0 and len(calls) == 1


@pytest.mark.asyncio
async def test_shared_cooldown_applies_to_other_roles_and_direct_text(monkeypatch, clock):
    llm, calls = sdk_client(monkeypatch, [remote_error(delay="5"), "text"], clock)
    with pytest.raises(LLMProviderFailure):
        await llm.complete(messages=[])
    assert await llm.complete(messages=[]) == "text"
    assert [when for when, _ in calls] == [0, 5]


@pytest.mark.asyncio
async def test_exponential_waits_and_actual_total_remain_bounded(monkeypatch, clock):
    llm, calls = sdk_client(monkeypatch, [remote_error() for _ in range(4)], clock)
    budget = DecisionBudget.reasoning_chain(3, 1)
    with pytest.raises(DecisionFailure) as caught:
        await StructuredDecisionClient(llm).ask(stage="any", payload={}, response_schema=SCHEMA,
                                              budget=budget)
    assert [when for when, _ in calls] == [0, 1, 3, 7]
    assert budget.used == 4 and budget.repair_used == budget.normal_used == 0
    assert caught.value.diagnostic.transport_used == 4
    assert caught.value.diagnostic.termination_reason == "provider_unavailable"


@pytest.mark.asyncio
async def test_total_wait_limit_is_not_reset_between_attempts(monkeypatch, clock):
    llm, calls = sdk_client(monkeypatch, [remote_error() for _ in range(3)], clock,
                           policy=ProviderRetryPolicy(max_wait_seconds=2))
    with pytest.raises(DecisionFailure):
        await StructuredDecisionClient(llm).ask(stage="any", payload={}, response_schema=SCHEMA,
                                              budget=DecisionBudget(6))
    assert len(calls) == 2 and clock[0] == 1


@pytest.mark.asyncio
async def test_guard_and_cancellation_during_wait_do_not_send_requests(clock):
    gate = ProviderBackoff(ProviderRetryPolicy())
    gate.failed(LLMProviderFailure(kind="rate_limited", retryable=True, retry_after=5))
    checks = 0

    async def guard():
        nonlocal checks
        checks += 1
        if checks == 3:
            raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await gate.wait(before_call=guard)
    assert checks == 3 and clock[0] == 1


@pytest.mark.asyncio
async def test_old_sibling_success_does_not_erase_cooldown(clock):
    gate = ProviderBackoff(ProviderRetryPolicy())
    gate.failed(LLMProviderFailure(kind="rate_limited", retryable=True, retry_after=5))
    gate.succeeded()
    assert await gate.wait() == 5


@pytest.mark.asyncio
async def test_success_after_transport_does_not_mislabel_later_normal_exhaustion(
    monkeypatch, clock,
):
    llm, _ = sdk_client(monkeypatch, [remote_error(), '{"answer":"ok"}'], clock)
    budget = DecisionBudget(2)
    decider = StructuredDecisionClient(llm)
    await decider.ask(stage="one", payload={}, response_schema=SCHEMA, budget=budget)
    with pytest.raises(DecisionFailure) as caught:
        await decider.ask(stage="two", payload={}, response_schema=SCHEMA, budget=budget)
    assert caught.value.kind == "budget"
    assert caught.value.diagnostic.termination_reason is None


def test_retry_policy_uses_existing_config_snapshot_and_rejects_invalid_values():
    config = LLMConfig.from_environment({
        "REDTEAM_LLM_PROVIDER": "litellm", "REDTEAM_LLM_RETRY_POLICY_JSON":
        '{"base_delay_seconds":2,"max_delay_seconds":10,"max_wait_seconds":0}',
    })
    assert config.provider_retry.base_delay_seconds == 2
    assert LLMConfig.model_validate_json(config.model_dump_json()) == config
    assert LLMConfig.from_environment({"REDTEAM_LLM_PROVIDER": "litellm"}).provider_retry == (
        ProviderRetryPolicy()
    )
    for bad in ['{"max_wait_seconds":-1}', '{"base_delay_seconds":NaN}',
                '{"extra":"private-config"}']:
        with pytest.raises(ValueError, match="REDTEAM_LLM_RETRY_POLICY_JSON is invalid") as caught:
            LLMConfig.from_environment({"REDTEAM_LLM_PROVIDER": "litellm",
                                        "REDTEAM_LLM_RETRY_POLICY_JSON": bad})
        assert "private-config" not in str(caught.value)
    with pytest.raises(ValidationError):
        ProviderRetryPolicy(base_delay_seconds=3, max_delay_seconds=1)


@pytest.mark.asyncio
async def test_provider_hold_preserves_graph_and_reports_unresolved(monkeypatch, clock, tmp_path):
    from redteam.app.cli.commands.report import _render_markdown
    from redteam.app.report_store import load_terminal_report, save_terminal_report
    from redteam.attack_graph.service import AttackGraphService
    from redteam.core.enums import AgentPhase, GraphNodeType
    from redteam.orchestration.engine import InvestigationEngine
    from redteam.orchestration.nodes.plan import plan_node
    from redteam.orchestration.nodes.replan import replan_node
    from redteam.orchestration.reporting import ReportingAgent
    from redteam.orchestration.state import RuntimeState, initial_graph_state
    from tests.fakes import build_test_application

    llm, calls = sdk_client(monkeypatch, [remote_error(delay="9000")], clock)
    deps = build_test_application(llm_client=llm)
    graph = AttackGraphService(deps.graph_store)
    await graph.upsert_node(GraphNodeType.HOST, key="fixture.local", attrs={"in_scope": True})
    before = await deps.graph_store.list_nodes()

    async def decide(self, runtime, observation, **kwargs):
        budget = DecisionBudget.reasoning_chain(3, 1)
        try:
            return await StructuredDecisionClient(llm).ask(
                stage="fixture-stage", payload={}, response_schema=SCHEMA, budget=budget,
            )
        finally:
            self.updated_runtime = runtime.model_copy(update={"model_requests_used": budget.used})

    monkeypatch.setattr(InvestigationEngine, "decide", decide)
    runtime = RuntimeState(run_id="run-provider-hold", engagement_id="eng-fixture",
                           phase=AgentPhase.PLAN, actions_executed=2)
    planned = await plan_node(initial_graph_state(runtime), deps=deps)
    final = await replan_node(planned, deps=deps)
    restored = RuntimeState.model_validate_json(final["runtime"].model_dump_json())
    assert restored.phase == AgentPhase.FINISHED and restored.planning_error is None
    assert restored.termination_reason == "provider_unavailable"
    assert restored.actions_executed == 2 and restored.model_requests_used == len(calls) == 1
    assert await deps.graph_store.list_nodes() == before
    report = await ReportingAgent().project(
        restored, graph=graph, evidence_store=deps.evidence_store, work_store=deps.work_store,
    )
    save_terminal_report(report, reports_dir=tmp_path)
    saved = load_terminal_report(restored.run_id, reports_dir=tmp_path)
    assert saved is not None and saved.run_outcome == "unresolved" and not saved.ready_for_release
    assert "llm_provider_rate_limited" in _render_markdown(saved)
    assert "private-provider-body" not in saved.model_dump_json() + _render_markdown(saved)


@pytest.mark.asyncio
@pytest.mark.parametrize("concurrency", [1, 2])
@pytest.mark.parametrize("code,delay", [("insufficient_quota", None), ("slow_down", "9000")])
async def test_worker_provider_failure_survives_settlement_without_another_loop(
    monkeypatch, clock, tmp_path, concurrency, code, delay,
):
    from dataclasses import replace

    from redteam.orchestration.delegated_runtime import decide_delegations
    from tests.unit.test_specialist_p2_reasoning import world

    llm, calls = sdk_client(monkeypatch, [remote_error(code, delay)], clock)
    _, deps, gateway, engine, path, runtime, context = await world(tmp_path, llm)
    engine.deps = replace(deps, settings=deps.settings.model_copy(update={
        "specialist_decision_concurrency": concurrency,
    }))
    with pytest.raises(DecisionFailure) as caught:
        await decide_delegations(engine, path, runtime, context)
    current = engine.updated_runtime
    assert caught.value.diagnostic.termination_reason == "provider_unavailable"
    assert current.model_requests_used == len(calls) == 1
    assert sum(d.decision_attempts_used for d in current.delegations.values()) == 1
    assert current.unresolved_evidence_ids
    assert gateway.invocations == []


@pytest.mark.asyncio
async def test_rate_limit_during_repair_keeps_original_validation_feedback(monkeypatch, clock):
    llm, calls = sdk_client(monkeypatch, [
        "bad JSON", remote_error(), '{"answer":"ok"}',
    ], clock)
    budget = DecisionBudget.reasoning_chain(2, 1)
    await StructuredDecisionClient(llm).ask(
        stage="any", payload={}, response_schema=SCHEMA, budget=budget,
    )
    assert calls[1][1]["messages"] == calls[2][1]["messages"]
    assert budget.repair_used == 1 and budget.transport_used == 1 and budget.used == 3
