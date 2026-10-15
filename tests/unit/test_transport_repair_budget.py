"""Transport retries and output repairs use distinct shared budget counters."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from redteam.orchestration.cognition import (
    DecisionBudget,
    DecisionFailure,
    DecisionValidationError,
    ReasoningHalted,
    StructuredDecisionClient,
)
from redteam.orchestration.diagnostics import DecisionDiagnostic

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


async def ask(client, budget, *, stage="transport-repair", **kwargs):
    return await client.ask(
        stage=stage, payload={"task": "bounded"}, response_schema=SCHEMA, budget=budget,
        **kwargs,
    )


async def test_transport_then_invalid_json_then_success_uses_each_counter_once():
    provider = FakeProvider([OSError("private transport text"), "not JSON", {"answer": "ok"}])
    client = StructuredDecisionClient(provider)
    budget = DecisionBudget.reasoning_chain(
        2, 2, transport_limit=1, total_limit=3,
    )

    assert await ask(client, budget) == {"answer": "ok"}

    assert budget.used == 3
    assert (budget.normal_used, budget.transport_used, budget.repair_used) == (1, 1, 1)
    assert all(call["num_retries"] == 0 for call in provider.calls)
    assert "Previous attempt failed" not in provider.calls[1]["messages"][0]["content"]
    assert (
        "Previous attempt failed (format): invalid_json"
        in provider.calls[2]["messages"][0]["content"]
    )
    assert "private transport text" not in repr(budget.diagnostic_details())


async def test_transport_retry_keeps_existing_repair_feedback_unchanged():
    provider = FakeProvider(["not JSON", OSError("private transport text"), {"answer": "ok"}])
    client = StructuredDecisionClient(provider, transport_limit=1)
    budget = DecisionBudget.reasoning_chain(
        2, 2, transport_limit=1, total_limit=3,
    )

    assert await ask(client, budget) == {"answer": "ok"}

    second_prompt = provider.calls[1]["messages"][0]["content"]
    third_prompt = provider.calls[2]["messages"][0]["content"]
    assert "Previous attempt failed (format): invalid_json" in second_prompt
    assert third_prompt == second_prompt
    assert "private transport text" not in third_prompt
    assert (budget.normal_used, budget.transport_used, budget.repair_used) == (1, 1, 1)


async def test_semantic_rejection_uses_repair_counter():
    provider = FakeProvider([{"answer": "bad"}, {"answer": "good"}])
    client = StructuredDecisionClient(provider)
    budget = DecisionBudget.reasoning_chain(2, 1, transport_limit=1)

    def validate(value):
        if value["answer"] != "good":
            raise DecisionValidationError(DecisionDiagnostic(
                code="semantic_rejection", stage="test", kind="semantic",
                summary="Use the accepted answer.", retryable=True,
            ))

    result = await client.ask(
        stage="semantic", payload={}, response_schema=SCHEMA, budget=budget,
        validate_response=validate,
    )

    assert result == {"answer": "good"}
    assert (budget.normal_used, budget.transport_used, budget.repair_used) == (1, 0, 1)


async def test_transport_cap_exhaustion_is_distinct_while_total_remains():
    provider = FakeProvider([OSError("one"), OSError("two")])
    budget = DecisionBudget.reasoning_chain(
        2, 2, transport_limit=1, total_limit=4,
    )

    with pytest.raises(DecisionFailure) as caught:
        await ask(StructuredDecisionClient(provider), budget)

    diagnostic = caught.value.diagnostic
    assert diagnostic.code == "decision_transport_budget_exhausted"
    assert (budget.used, budget.remaining) == (2, 2)
    assert (diagnostic.transport_limit, diagnostic.transport_used) == (1, 1)
    assert diagnostic.repair_used == 0


async def test_repair_cap_exhaustion_is_distinct_while_total_remains():
    provider = FakeProvider(["not JSON", "still not JSON"])
    budget = DecisionBudget.reasoning_chain(
        2, 1, transport_limit=1, total_limit=4,
    )

    with pytest.raises(DecisionFailure) as caught:
        await ask(StructuredDecisionClient(provider), budget)

    assert caught.value.diagnostic.code == "decision_repair_budget_exhausted"
    assert budget.remaining == 1
    assert (budget.normal_used, budget.repair_used, budget.transport_used) == (1, 1, 0)


async def test_shared_budget_counters_do_not_reset_between_stages():
    provider = FakeProvider([
        OSError("temporary"), {"answer": "stage one"}, "not JSON", {"answer": "stage two"},
    ])
    client = StructuredDecisionClient(provider)
    budget = DecisionBudget.reasoning_chain(
        3, 2, transport_limit=1, total_limit=4,
    )

    assert await ask(client, budget, stage="first-stage") == {"answer": "stage one"}
    assert (budget.normal_used, budget.transport_used, budget.repair_used) == (1, 1, 0)
    assert await ask(client, budget, stage="second-stage") == {"answer": "stage two"}

    assert budget.used == 4
    assert (budget.normal_used, budget.transport_used, budget.repair_used) == (2, 1, 1)
    assert budget.stage_attempts == {"first-stage": 2, "second-stage": 2}


async def test_transport_limit_is_forwarded_to_internal_budget():
    provider = FakeProvider([OSError("one"), OSError("two")])

    with pytest.raises(DecisionFailure) as caught:
        await StructuredDecisionClient(provider, max_attempts=4, transport_limit=1).ask(
            stage="internal-budget", payload={}, response_schema=SCHEMA,
        )

    assert caught.value.diagnostic.code == "decision_transport_budget_exhausted"
    assert caught.value.diagnostic.transport_limit == 1
    assert caught.value.diagnostic.transport_used == 1
    assert len(provider.calls) == 2


async def test_bare_client_defaults_to_transport_accounting():
    provider = FakeProvider([OSError(str(index)) for index in range(6)])

    with pytest.raises(DecisionFailure) as caught:
        await StructuredDecisionClient(provider, max_attempts=8).ask(
            stage="default-transport-budget", payload={}, response_schema=SCHEMA,
        )

    diagnostic = caught.value.diagnostic
    assert diagnostic.code == "decision_transport_budget_exhausted"
    assert (diagnostic.transport_limit, diagnostic.transport_used) == (5, 5)
    assert (diagnostic.normal_used, diagnostic.repair_used) == (1, 0)
    assert len(provider.calls) == 6


async def test_native_timeout_retries_as_transport_not_repair():
    provider = FakeProvider(
        native_responses=[BLOCK, SimpleNamespace(tool_calls=[], total_tokens=2)]
    )
    client = StructuredDecisionClient(
        provider, llm_attempt_timeout_seconds=0.01, transport_limit=1,
    )
    budget = DecisionBudget.reasoning_chain(2, 1, transport_limit=1, total_limit=3)

    result = await client.ask_native(messages=[], tools=[], budget=budget)

    assert result.tool_calls == []
    assert budget.used == 2
    assert (budget.normal_used, budget.transport_used, budget.repair_used) == (1, 1, 0)
    assert budget.failure_counts["llm_request_timeout"] == 1
    assert all(call["num_retries"] == 0 for call in provider.native_calls)


async def test_guard_denial_does_not_consume_retry_counters():
    provider = FakeProvider()
    budget = DecisionBudget.reasoning_chain(2, 2, transport_limit=1)

    async def deny():
        raise ReasoningHalted("run_budget_exhausted")

    with pytest.raises(DecisionFailure) as caught:
        await ask(StructuredDecisionClient(provider), budget, stage="guarded", before_call=deny)

    assert caught.value.kind == "budget"
    assert caught.value.diagnostic.code == "reasoning_guard_blocked"
    assert budget.used == budget.normal_used == budget.repair_used == budget.transport_used == 0
    assert provider.calls == []


async def test_legacy_explicit_six_attempt_client_total_is_unchanged():
    provider = FakeProvider([OSError(str(index)) for index in range(6)])

    with pytest.raises(DecisionFailure) as caught:
        await StructuredDecisionClient(provider, transport_limit=None).ask(
            stage="legacy-total", payload={}, response_schema=SCHEMA,
        )

    assert caught.value.diagnostic.code == "decision_budget_exhausted"
    assert caught.value.diagnostic.attempt_limit == 6
    assert caught.value.diagnostic.attempts == 6
    assert len(provider.calls) == 6


def test_transport_limit_does_not_expand_default_chain_total():
    budget = DecisionBudget.reasoning_chain(12, 6, transport_limit=5)

    assert budget.max_attempts == 18
    assert budget.remaining == 18
