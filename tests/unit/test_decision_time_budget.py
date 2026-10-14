"""Absolute chain deadlines and bounded LLM attempts share one budget."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import redteam.orchestration.analysis_budget as analysis_budget
import redteam.orchestration.cognition as cognition
from redteam.orchestration.cognition import (
    DecisionBudget,
    DecisionFailure,
    StructuredDecisionClient,
)

SCHEMA = {
    "type": "object",
    "properties": {"answer": {"type": "string"}},
    "required": ["answer"],
    "additionalProperties": False,
}

BLOCK = object()


class FakeProvider:
    def __init__(self, responses=None, *, native_responses=None) -> None:
        self.responses = list(responses or [])
        self.native_responses = list(native_responses or [])
        self.calls: list[dict] = []
        self.native_calls: list[dict] = []
        self.started = asyncio.Event()

    async def complete_structured(self, **kwargs):
        self.calls.append(kwargs)
        self.started.set()
        value = self.responses.pop(0) if self.responses else {"answer": "ok"}
        if value is BLOCK:
            await asyncio.Event().wait()
        if isinstance(value, BaseException):
            raise value
        return value

    async def complete_native(self, **kwargs):
        self.native_calls.append(kwargs)
        self.started.set()
        value = self.native_responses.pop(0) if self.native_responses else SimpleNamespace(
            tool_calls=[], total_tokens=1,
        )
        if value is BLOCK:
            await asyncio.Event().wait()
        if isinstance(value, BaseException):
            raise value
        return value


def request(client: StructuredDecisionClient, *, budget: DecisionBudget):
    return client.ask(
        stage="time-budget",
        payload={},
        response_schema=SCHEMA,
        budget=budget,
    )


async def test_expired_deadline_rejects_ask_and_native_without_provider_calls(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(cognition.time, "monotonic", lambda: now[0])
    budget = DecisionBudget.reasoning_chain(2, 1, timeout_seconds=1.0)
    now[0] = 102.0
    provider = FakeProvider()
    client = StructuredDecisionClient(
        provider, decision_timeout_seconds=10.0, llm_attempt_timeout_seconds=1.0,
    )

    with pytest.raises(DecisionFailure) as ask_failure:
        await request(client, budget=budget)
    assert ask_failure.value.kind == "budget"
    assert ask_failure.value.diagnostic.code == "decision_time_budget_exhausted"
    assert provider.calls == []

    with pytest.raises(DecisionFailure) as native_failure:
        await client.ask_native(messages=[], tools=[], budget=budget)
    assert native_failure.value.kind == "budget"
    assert native_failure.value.diagnostic.code == "decision_time_budget_exhausted"
    assert provider.native_calls == []


async def test_success_on_eighteenth_and_last_chain_attempt():
    replies = []
    for index in range(6):
        replies.extend(["invalid JSON", {"answer": f"repair-{index}"}])
    replies.extend({"answer": f"normal-{index}"} for index in range(6))
    provider = FakeProvider(replies)
    client = StructuredDecisionClient(
        provider, decision_timeout_seconds=10.0, llm_attempt_timeout_seconds=1.0,
    )
    budget = DecisionBudget.reasoning_chain(12, 6, timeout_seconds=5.0)

    result = None
    for _ in range(12):
        result = await request(client, budget=budget)

    assert result == {"answer": "normal-5"}
    assert budget.used == 18
    assert (budget.normal_used, budget.repair_used) == (12, 6)
    assert len(provider.calls) == 18
    assert all(call["num_retries"] == 0 for call in provider.calls)


async def test_reusing_budget_does_not_reset_deadline_and_fresh_budget_does(monkeypatch):
    now = [200.0]
    monkeypatch.setattr(cognition.time, "monotonic", lambda: now[0])
    provider = FakeProvider()
    client = StructuredDecisionClient(
        provider, decision_timeout_seconds=10.0, llm_attempt_timeout_seconds=1.0,
    )
    shared = DecisionBudget(3, timeout_seconds=2.0)
    assert await request(client, budget=shared) == {"answer": "ok"}
    first_call_count = len(provider.calls)

    now[0] = 203.0
    with pytest.raises(DecisionFailure) as caught:
        await request(client, budget=shared)
    assert caught.value.diagnostic.code == "decision_time_budget_exhausted"
    assert len(provider.calls) == first_call_count

    fresh = DecisionBudget(1, timeout_seconds=2.0)
    assert await request(client, budget=fresh) == {"answer": "ok"}
    assert len(provider.calls) == first_call_count + 1


async def test_per_attempt_timeout_uses_one_shared_attempt_then_repairs():
    provider = FakeProvider([BLOCK, {"answer": "recovered"}])
    client = StructuredDecisionClient(
        provider, max_attempts=9, decision_timeout_seconds=2.0,
        llm_attempt_timeout_seconds=0.02,
    )
    budget = DecisionBudget.reasoning_chain(2, 1, timeout_seconds=1.0)

    assert await request(client, budget=budget) == {"answer": "recovered"}
    assert len(provider.calls) == 2
    assert budget.used == 2
    assert (budget.normal_used, budget.repair_used) == (1, 1)
    assert budget.failure_counts["llm_request_timeout"] == 1
    assert budget.last_failure is not None
    assert (budget.last_failure.kind, budget.last_failure.code) == (
        "transport", "llm_request_timeout",
    )
    assert budget.unknown_usage_count == 2
    assert all(call["num_retries"] == 0 for call in provider.calls)


async def test_chained_asks_cannot_duplicate_the_shared_attempt_allowance():
    provider = FakeProvider(["invalid JSON"] * 5)
    client = StructuredDecisionClient(
        provider, decision_timeout_seconds=3.0, llm_attempt_timeout_seconds=1.0,
    )
    budget = DecisionBudget.reasoning_chain(2, 2, total_limit=3, timeout_seconds=2.0)

    with pytest.raises(DecisionFailure):
        await request(client, budget=budget)
    with pytest.raises(DecisionFailure) as next_call:
        await request(client, budget=budget)

    assert next_call.value.kind == "budget"
    assert len(provider.calls) == budget.used == 3
    assert budget.remaining == 0


async def test_cancellation_propagates_from_pending_provider_unchanged():
    provider = FakeProvider([BLOCK])
    client = StructuredDecisionClient(
        provider, decision_timeout_seconds=5.0, llm_attempt_timeout_seconds=3.0,
    )
    budget = DecisionBudget(2)
    task = asyncio.create_task(request(client, budget=budget))
    await provider.started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(provider.calls) == 1
    assert budget.used == 1


async def test_native_timeout_is_retryable_inside_same_budget_and_disables_sdk_retries():
    provider = FakeProvider(
        native_responses=[BLOCK, SimpleNamespace(tool_calls=[], total_tokens=2)]
    )
    client = StructuredDecisionClient(
        provider, max_attempts=8, decision_timeout_seconds=2.0,
        llm_attempt_timeout_seconds=0.02,
    )
    budget = DecisionBudget.reasoning_chain(2, 1, timeout_seconds=1.0)

    result = await client.ask_native(messages=[], tools=[], budget=budget)

    assert result.tool_calls == []
    assert len(provider.native_calls) == budget.used == 2
    assert budget.failure_counts["llm_request_timeout"] == 1
    assert all(call["num_retries"] == 0 for call in provider.native_calls)


async def test_native_cancellation_propagates_from_pending_provider_unchanged():
    provider = FakeProvider(native_responses=[BLOCK])
    client = StructuredDecisionClient(
        provider, decision_timeout_seconds=5.0, llm_attempt_timeout_seconds=3.0,
    )
    budget = DecisionBudget(2)
    task = asyncio.create_task(client.ask_native(messages=[], tools=[], budget=budget))
    await provider.started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(provider.native_calls) == 1
    assert budget.used == 1


async def test_analysis_verification_budget_carries_the_absolute_deadline(monkeypatch):
    now = [300.0]
    monkeypatch.setattr(cognition.time, "monotonic", lambda: now[0])
    deps = SimpleNamespace(settings=SimpleNamespace(
        decision_max_attempts=3, decision_timeout_seconds=1.0,
    ))
    state = {"runtime": SimpleNamespace(delegations={})}
    budget = analysis_budget.verification_budget(state, deps=deps)
    now[0] = 302.0
    provider = FakeProvider()
    client = StructuredDecisionClient(
        provider, decision_timeout_seconds=5.0, llm_attempt_timeout_seconds=1.0,
    )

    with pytest.raises(DecisionFailure) as caught:
        await request(client, budget=budget)

    assert caught.value.kind == "budget"
    assert caught.value.diagnostic.code == "decision_time_budget_exhausted"
    assert provider.calls == []


async def test_analysis_engagement_time_guard_denial_stays_budget_and_is_not_retried(
    monkeypatch,
):
    class LegacyChange:
        def legacy_scope(self, run_id: str) -> None:
            assert run_id == "run-analysis-budget"

    class Coordinator:
        async def change(self, callback):
            return await callback(LegacyChange())

    monkeypatch.setattr(
        analysis_budget, "reasoning_time_allowed", lambda *_args, **_kwargs: False,
    )
    deps = SimpleNamespace(
        settings=SimpleNamespace(decision_max_attempts=3, decision_timeout_seconds=10.0),
        central_state=None,
        coordinator=Coordinator(),
        engagement=object(),
    )
    runtime = SimpleNamespace(
        run_id="run-analysis-budget", engagement_id="engagement",
        mission_id=None, is_terminal=False, termination_reason=None, delegations={},
    )
    state = {"runtime": runtime}
    budget = analysis_budget.verification_budget(state, deps=deps)
    guard = analysis_budget.verification_guard(state, deps=deps, budget=budget)
    provider = FakeProvider()
    client = StructuredDecisionClient(
        provider, decision_timeout_seconds=10.0, llm_attempt_timeout_seconds=1.0,
    )

    with pytest.raises(DecisionFailure) as caught:
        await client.ask(
            stage="analysis-review", payload={}, response_schema=SCHEMA,
            budget=budget, before_call=guard,
        )

    assert caught.value.kind == "budget"
    assert caught.value.diagnostic.code == "reasoning_guard_blocked"
    assert caught.value.diagnostic.summary == "analysis_engagement_time_exhausted"
    assert budget.used == 0
    assert provider.calls == []
