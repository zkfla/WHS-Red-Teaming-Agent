"""Provider usage survives format repair and remains local to each request."""

from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from redteam.llm.litellm import LiteLLMClient
from redteam.orchestration.cognition import DecisionBudget, StructuredDecisionClient

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
