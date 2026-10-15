"""Request-local telemetry through adapters and the existing durable ledger.

All providers are explicit in-process doubles; no credentials or network calls.
"""

import asyncio
import sys
from dataclasses import asdict
from types import SimpleNamespace

import pytest

from redteam.core.contracts.llm_policy import LLMRequestPolicy
from redteam.core.ports.llm import LLMProviderFailure, LLMUsage, StructuredCompletion
from redteam.llm.litellm import LiteLLMClient, _extract_usage
from redteam.orchestration.cognition import DecisionBudget, StructuredDecisionClient
from redteam.orchestration.llm_requests import RequestAdmissionError, RunRequestLedger
from redteam.orchestration.metered_llm import RunMeteredLLM, llm_request_scope
from redteam.orchestration.prompts import instruction_scope
from tests.unit.test_llm_usage import SCHEMA
from tests.unit.test_orchestration_coordination import RUN, world


@pytest.mark.parametrize("reported,expected", [
    (None, None), ({"total_tokens": 9}, LLMUsage(total_tokens=9)),
    ({"prompt_tokens": 10, "completion_tokens": 2,
      "prompt_tokens_details": {"cached_tokens": 6, "cache_creation_tokens": 3}},
     LLMUsage(10, 2, 12, 6, 3)),
    ({"input_tokens": 10, "output_tokens": 2,
      "input_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 0}},
     LLMUsage(10, 2, 12, 0, 0)),
    ({"input_tokens": 10}, LLMUsage(input_tokens=10)),
    ({"input_tokens": True, "output_tokens": -1, "total_tokens": "12",
      "input_tokens_details": {"cached_tokens": False}}, LLMUsage()),
])
def test_normalized_usage_unknown_zero_and_inclusive_cache(reported, expected):
    assert _extract_usage({"usage": reported}) == expected
    if reported is not None:
        # Both mapping and object SDK shapes are supported.
        assert _extract_usage(SimpleNamespace(usage=SimpleNamespace(**reported))) == expected


def sdk(monkeypatch, responses, *, subscription=False):
    captured = []

    async def dispatch(**kwargs):
        captured.append(kwargs)
        response = responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response

    monkeypatch.setitem(sys.modules, "litellm", SimpleNamespace(
        get_llm_provider=lambda model: (model, "chatgpt" if subscription else "openai", None, None),
        acompletion=dispatch, aresponses=dispatch, model_cost={}, register_model=lambda _: None,
    ))
    return captured


def chat(text, usage):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
        content=text, tool_calls=None,
    ))], usage=usage)


@pytest.mark.parametrize("method", ["complete_with_usage", "complete_structured_with_usage",
                                        "complete_native"])
async def test_chat_all_result_shapes_carry_same_request_usage(monkeypatch, method):
    counts = {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12,
              "prompt_tokens_details": {"cached_tokens": 6, "cache_write_tokens": 3}}
    captured = sdk(monkeypatch, [chat('{"answer":"ok"}', counts)])
    client = LiteLLMClient("unit-model")
    extra = {"tools": []} if method == "complete_native" else (
        {"response_schema": SCHEMA} if method == "complete_structured_with_usage" else {})
    result = await getattr(client, method)(messages=[], system="Stable instructions", **extra)
    assert result.usage == LLMUsage(10, 2, 12, 6, 3)
    assert result.total_tokens == 12  # cache is already INCLUDED
    assert len(captured) == 1
    assert captured[0]["messages"][0] == {"role": "system", "content": "Stable instructions"}


async def events(usage, *, completed=True):
    yield {"type": "response.output_text.delta", "delta": '{"answer":"ok"}'}
    if completed:
        yield {"type": "response.completed", "response": {
            "status": "completed", "output": [], "usage": usage,
        }}


@pytest.mark.parametrize("method", ["complete_with_usage", "complete_structured_with_usage",
                                        "complete_native"])
@pytest.mark.parametrize("streaming", [False, True])
async def test_responses_terminal_usage_for_text_structured_native(monkeypatch, method, streaming):
    counts = {"input_tokens": 10, "output_tokens": 2, "total_tokens": 12,
              "input_tokens_details": {"cached_tokens": 0}}
    response = events(counts) if streaming else {"status": "completed", "usage": counts,
        "output": [{"type": "message", "content": [
            {"type": "output_text", "text": '{"answer":"ok"}'}]}]}
    captured = sdk(monkeypatch, [response], subscription=streaming)
    client = LiteLLMClient("unit-model", use_responses_api=not streaming)
    extra = {"tools": []} if method == "complete_native" else (
        {"response_schema": SCHEMA} if method == "complete_structured_with_usage" else {})
    result = await getattr(client, method)(messages=[], **extra)
    assert result.usage == LLMUsage(10, 2, 12, 0)
    assert result.total_tokens == 12
    assert len(captured) == 1


@pytest.mark.parametrize("completed", [False, True])
@pytest.mark.parametrize("method", ["complete", "complete_native"])
async def test_stream_missing_usage_and_interrupted_dispatch_remain_charged(
    tmp_path, monkeypatch, completed, method,
):
    sdk(monkeypatch, [events(None, completed=completed)], subscription=True)
    w = await world(tmp_path, "sqlite")
    ledger = RunRequestLedger(w.coordinator)
    await ledger.bind(RUN, LLMRequestPolicy(mode="bounded", max_requests=1))
    client = RunMeteredLLM(LiteLLMClient("unit-model"), ledger)
    extra = {"tools": []} if method == "complete_native" else {}
    with llm_request_scope(RUN, "worker"), instruction_scope("probe_execution"):
        if completed:
            result = await getattr(client, method)(messages=[], **extra)
            assert (result if isinstance(result, str) else result.text) == '{"answer":"ok"}'
        else:
            with pytest.raises(LLMProviderFailure):
                await getattr(client, method)(messages=[], **extra)
    usage = await ledger.snapshot(RUN)
    assert usage["spent"] == usage["unknown_usage"] == 1
    assert usage["unknown_dispatch"] == (0 if completed else 1)
    assert usage["usage"]["cache_read_tokens"] == {"known": 0, "unknown": 1}
    with pytest.raises(RequestAdmissionError, match="run_llm_request_limit_exhausted"):
        await ledger.reserve(RUN, owner="replacement", kind="complete")


async def test_concurrent_request_usage_and_durable_attribution_are_isolated(tmp_path):
    w = await world(tmp_path, "sqlite")
    ledger = RunRequestLedger(w.coordinator)
    await ledger.bind(RUN, LLMRequestPolicy(mode="unlimited_requests", max_concurrent_requests=3))

    class Provider:
        async def complete_with_usage(self, *, messages, **kwargs):
            index = messages[0]["index"]
            await asyncio.sleep(0)
            return StructuredCompletion("ok", usage=LLMUsage(index, 2, index + 2, index - 1))

    client = RunMeteredLLM(Provider(), ledger)

    async def invoke(index):
        with llm_request_scope(RUN, f"worker-{index}"), instruction_scope(f"stage-{index}"):
            assert await client.complete(messages=[{"index": index}]) == "ok"

    await asyncio.gather(invoke(1), invoke(2))
    usage = await RunRequestLedger(w.coordinator).snapshot(RUN)
    assert usage["known_tokens"] == 7
    assert usage["usage"]["input_tokens"] == {"known": 3, "unknown": 0}
    for index in (1, 2):
        expected = {"known": index - 1, "unknown": 0}
        assert usage["by_owner"][f"worker-{index}"]["usage"]["cache_read_tokens"] == expected
        assert usage["by_stage"][f"stage-{index}"]["usage"]["cache_read_tokens"] == expected
    # Existing legacy rows and grouping, with no guessed Task attribution.
    request = await ledger.reserve(RUN, owner="legacy", kind="complete")
    await ledger.dispatch(RUN, request)
    await ledger.settle(RUN, request, total_tokens=5)
    usage = await ledger.snapshot(RUN)
    assert usage["usage"]["cache_read_tokens"] == {"known": 1, "unknown": 1}
    assert usage["by_task"]["unknown"]["spent"] == 3


async def test_repair_settles_each_request_before_parsing_and_preserves_cache_usage(
    tmp_path, monkeypatch,
):
    sdk(monkeypatch, [chat("invalid json", {"total_tokens": 4}),
        chat('{"answer":"ok"}', {"prompt_tokens": 6, "completion_tokens": 2,
                                  "prompt_tokens_details": {"cached_tokens": 0}})])
    w = await world(tmp_path, "sqlite")
    ledger = RunRequestLedger(w.coordinator)
    await ledger.bind(RUN, LLMRequestPolicy(mode="bounded", max_requests=2))
    client = RunMeteredLLM(LiteLLMClient("unit-model"), ledger)
    with llm_request_scope(RUN, "planner"):
        result = await StructuredDecisionClient(client).ask(
            stage="usage-repair", payload={}, response_schema=SCHEMA,
            budget=DecisionBudget.reasoning_chain(1, 1),
        )
    assert result == {"answer": "ok"}
    snapshot = await ledger.snapshot(RUN)
    assert snapshot["spent"] == 2 and snapshot["known_tokens"] == 12
    assert snapshot["usage"]["cache_read_tokens"] == {"known": 0, "unknown": 1}


async def test_usage_integrity_and_idempotent_settlement(tmp_path):
    w = await world(tmp_path, "sqlite")
    ledger = RunRequestLedger(w.coordinator)
    await ledger.bind(RUN, LLMRequestPolicy(mode="unlimited_requests"))
    request = await ledger.reserve(RUN, owner="worker", kind="native")
    await ledger.dispatch(RUN, request)
    counts = LLMUsage(10, 2, 12, 6, 3)
    await ledger.settle(RUN, request, usage=counts)
    await ledger.settle(RUN, request, usage=counts)
    with pytest.raises(RequestAdmissionError, match="settlement_conflict"):
        await ledger.settle(RUN, request, total_tokens=12)  # cannot erase known details
    with pytest.raises(ValueError, match="total mismatch"):
        StructuredCompletion("ok", total_tokens=13, usage=counts)
    async def corrupt(tx):
        tx.records[("llm_request", request)]["usage"] = {
            **asdict(counts), "cache_read_tokens": True,
        }
    await w.coordinator.change(corrupt)
    with pytest.raises(RequestAdmissionError, match="record_invalid"):
        await ledger.snapshot(RUN)
