"""Native tool-calling adapter tests (offline, no provider credentials)."""

from __future__ import annotations

import dataclasses
import json
import sys
from types import SimpleNamespace

import pytest

from redteam.core.ports.llm import (
    NATIVE_ARG_INVALID_ARGUMENTS,
    NATIVE_PROTOCOL_EMPTY_RESPONSE,
    NATIVE_PROTOCOL_MISSING_CALL_ID,
    NativeCompletion,
    NativeToolCall,
    NativeToolSpec,
)
from redteam.llm import litellm as adapter
from redteam.llm.fake import FakeLLMClient
from redteam.llm.litellm import LiteLLMClient

SPEC = NativeToolSpec(
    name="lookup", description="look up", input_schema={"type": "object"}
)


def _chat_namespace(monkeypatch, message, total=None):
    async def acompletion(**kwargs):
        acompletion.kwargs = kwargs
        usage = {} if total is None else {"total_tokens": total}
        return SimpleNamespace(
            choices=[SimpleNamespace(message=message)], usage=usage
        )

    fake = SimpleNamespace(
        get_llm_provider=lambda model: (model, "openai", None, None),
        acompletion=acompletion,
    )
    monkeypatch.setitem(sys.modules, "litellm", fake)
    return acompletion


def _chat_message(content="", calls=None):
    return SimpleNamespace(content=content, tool_calls=calls)


def _chat_call(call_id, name, arguments):
    return SimpleNamespace(
        id=call_id, function=SimpleNamespace(name=name, arguments=arguments)
    )


def test_native_records_are_frozen():
    spec = NativeToolSpec(name="a", description="d", input_schema={"type": "object"})
    with pytest.raises(dataclasses.FrozenInstanceError):
        spec.name = "b"  # type: ignore[misc]
    call = NativeToolCall(call_id="c1", name="a", arguments={})
    with pytest.raises(dataclasses.FrozenInstanceError):
        call.name = "b"  # type: ignore[misc]
    completion = NativeCompletion(tool_calls=(call,), text="hi")
    with pytest.raises(dataclasses.FrozenInstanceError):
        completion.text = "x"  # type: ignore[misc]


def test_native_spec_rejects_empty_name():
    with pytest.raises(ValueError):
        NativeToolSpec(name="", description="d", input_schema={})


def test_native_call_invalid_arguments_never_keeps_raw():
    call = NativeToolCall(
        call_id="c1", name="lookup", arguments=None,
        argument_error=NATIVE_ARG_INVALID_ARGUMENTS,
    )
    assert call.arguments is None
    assert call.argument_error == NATIVE_ARG_INVALID_ARGUMENTS


def test_native_completion_caps_public_text_and_unknown_usage():
    completion = NativeCompletion(
        tool_calls=(), text="x" * 30000, total_tokens=-5,
        protocol_error=NATIVE_PROTOCOL_EMPTY_RESPONSE,
    )
    assert len(completion.text) == 20000
    assert completion.total_tokens is None
    with pytest.raises(ValueError):
        NativeCompletion(tool_calls=(), text="", protocol_error="bogus-code")


def test_native_completion_has_no_reasoning_field():
    assert not hasattr(NativeCompletion(tool_calls=(), text="hi"), "reasoning")


async def test_chat_valid_calls_preserved_with_usage(monkeypatch):
    message = _chat_message(
        "go", calls=[_chat_call("call_1", "lookup", '{"q": "a"}')]
    )
    stub = _chat_namespace(monkeypatch, message, total=7)
    result = await LiteLLMClient("unit-model").complete_native(
        [{"role": "user", "content": "hi"}], [SPEC]
    )
    assert isinstance(result, NativeCompletion)
    assert result.protocol_error is None
    assert result.total_tokens == 7
    assert result.text == "go"
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0].call_id == "call_1"
    assert result.tool_calls[0].arguments == {"q": "a"}
    assert result.tool_calls[0].argument_error is None
    assert stub.kwargs["tools"][0]["function"]["name"] == "lookup"


async def test_chat_malformed_arguments_keep_usable_sibling(monkeypatch):
    message = _chat_message(
        "",
        calls=[
            _chat_call("call_1", "lookup", "{not json"),
            _chat_call("call_2", "lookup", '{"q": "b"}'),
        ],
    )
    _chat_namespace(monkeypatch, message)
    result = await LiteLLMClient("unit-model").complete_native(
        [{"role": "user", "content": "hi"}], [SPEC]
    )
    assert result.protocol_error is None
    by_id = {call.call_id: call for call in result.tool_calls}
    assert set(by_id) == {"call_1", "call_2"}
    assert by_id["call_1"].arguments is None
    assert by_id["call_1"].argument_error == NATIVE_ARG_INVALID_ARGUMENTS
    assert by_id["call_2"].arguments == {"q": "b"}
    assert by_id["call_2"].argument_error is None


async def test_chat_missing_id_is_protocol_error_without_invention(monkeypatch):
    message = _chat_message(
        "",
        calls=[{"function": {"name": "lookup", "arguments": "{}"}}],
    )
    _chat_namespace(monkeypatch, message)
    result = await LiteLLMClient("unit-model").complete_native(
        [{"role": "user", "content": "hi"}], [SPEC]
    )
    assert result.tool_calls == ()
    assert result.protocol_error == NATIVE_PROTOCOL_MISSING_CALL_ID
    assert "call_0" not in result.text


async def test_chat_text_without_calls_is_terminal_not_success(monkeypatch):
    _chat_namespace(monkeypatch, _chat_message("finished", calls=None))
    result = await LiteLLMClient("unit-model").complete_native(
        [{"role": "user", "content": "hi"}], [SPEC]
    )
    assert result.tool_calls == ()
    assert result.text == "finished"
    assert result.protocol_error is None


async def test_chat_empty_response_is_protocol_error(monkeypatch):
    _chat_namespace(monkeypatch, _chat_message("", calls=None))
    result = await LiteLLMClient("unit-model").complete_native(
        [{"role": "user", "content": "hi"}], [SPEC]
    )
    assert result.tool_calls == ()
    assert result.protocol_error == NATIVE_PROTOCOL_EMPTY_RESPONSE


async def test_chat_drops_previous_response_id(monkeypatch):
    stub = _chat_namespace(monkeypatch, _chat_message("done", calls=None))
    await LiteLLMClient("unit-model").complete_native(
        [{"role": "user", "content": "hi"}], [SPEC],
        previous_response_id="resp_private",
    )
    assert "previous_response_id" not in stub.kwargs


def test_chat_tool_param_shape():
    params = adapter._chat_tools_param([SPEC])
    assert params is not None
    assert params[0]["type"] == "function"
    assert params[0]["function"]["name"] == "lookup"
    assert params[0]["function"]["parameters"] == {"type": "object"}
    assert adapter._chat_tools_param([]) is None


def test_responses_tool_param_shape():
    params = adapter._responses_tools_param([SPEC])
    assert params is not None
    assert params[0]["type"] == "function"
    assert params[0]["name"] == "lookup"
    assert params[0]["parameters"] == {"type": "object"}


def test_responses_input_converts_tool_roundtrip():
    messages = [
        {"role": "user", "content": "investigate"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_9",
                    "type": "function",
                    "function": {"name": "lookup", "arguments": json.dumps({"q": 1})},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_9", "content": "evidence"},
    ]
    converted = adapter._responses_input_from_chat(messages)
    kinds = [item.get("type", item.get("role")) for item in converted]
    assert "user" in kinds
    assert "function_call" in kinds
    assert "function_call_output" in kinds
    call = next(item for item in converted if item.get("type") == "function_call")
    assert call["call_id"] == "call_9"
    assert call["name"] == "lookup"
    assert json.loads(call["arguments"]) == {"q": 1}
    assert "id" not in call
    output = next(
        item for item in converted if item.get("type") == "function_call_output"
    )
    assert output["call_id"] == "call_9"
    assert output["output"] == "evidence"
    assert "previous_response_id" not in json.dumps(converted)


class _SubscriptionHarness:
    def __init__(self, monkeypatch, response):
        import litellm

        self.captured: dict = {}

        async def aresponses(**kwargs):
            self.captured = kwargs
            return response

        monkeypatch.setattr(
            litellm, "get_llm_provider", lambda model: (model, "chatgpt", None, None)
        )
        monkeypatch.setattr(litellm, "aresponses", aresponses)


async def test_subscription_nonstream_roundtrip(monkeypatch):
    response = SimpleNamespace(
        status="completed",
        output=[
            {
                "type": "message",
                "content": [{"type": "output_text", "text": "working"}],
            },
            {
                "type": "function_call",
                "call_id": "call_3",
                "name": "lookup",
                "arguments": '{"q": "x"}',
            },
        ],
    )
    harness = _SubscriptionHarness(monkeypatch, response)
    result = await LiteLLMClient("chatgpt/unit-model").complete_native(
        [{"role": "user", "content": "hi"}], [SPEC], system="sys",
        **{"stream": False, "previous_response_id": "resp_private"},
    )
    assert result.protocol_error is None
    assert result.text == "working"
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0].call_id == "call_3"
    assert result.tool_calls[0].arguments == {"q": "x"}
    assert harness.captured["instructions"] == "sys"
    assert harness.captured["tools"][0]["name"] == "lookup"
    assert "previous_response_id" not in harness.captured


async def test_subscription_stream_accumulates_calls_and_ignores_reasoning():
    async def stream():
        yield {"type": "response.output_item.added", "output_index": 0,
               "item_id": "it1", "item": {"type": "function_call",
               "call_id": "call_7", "name": "lookup", "arguments": ""}}
        yield {"type": "response.function_call_arguments.delta", "output_index": 0,
               "item_id": "it1", "delta": '{"q": "v"}'[:6]}
        yield {"type": "response.reasoning_summary_text.delta", "output_index": 0,
               "item_id": "rs1", "summary_index": 0, "delta": "private-plan"}
        yield {"type": "response.function_call_arguments.delta", "output_index": 0,
               "item_id": "it1", "delta": '{"q": "v"}'[6:]}
        yield {"type": "response.output_text.delta", "output_index": 1,
               "item_id": "m1", "content_index": 0, "delta": "public "}
        yield {"type": "response.output_text.delta", "output_index": 1,
               "item_id": "m1", "content_index": 0, "delta": "note"}
        yield {"type": "response.completed"}

    client = LiteLLMClient.__new__(LiteLLMClient)
    result = await client._accumulate_responses_stream_native(stream())
    assert result.protocol_error is None
    assert result.text == "public note"
    assert "private-plan" not in result.text
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0].call_id == "call_7"
    assert result.tool_calls[0].arguments == {"q": "v"}
    assert not hasattr(result, "reasoning")


async def test_subscription_stream_missing_id_reports_protocol_error():
    async def stream():
        yield {"type": "response.output_item.added", "output_index": 0,
               "item_id": "it1", "item": {"type": "function_call",
               "name": "lookup", "arguments": "{}"}}
        yield {"type": "response.function_call_arguments.done", "output_index": 0,
               "item_id": "it1", "arguments": "{}"}
        yield {"type": "response.completed"}

    client = LiteLLMClient.__new__(LiteLLMClient)
    result = await client._accumulate_responses_stream_native(stream())
    assert result.tool_calls == ()
    assert result.protocol_error == NATIVE_PROTOCOL_MISSING_CALL_ID


async def test_subscription_stream_without_completed_is_provider_failure():
    from redteam.core.ports.llm import LLMProviderFailure

    async def stream():
        yield {"type": "response.output_text.delta", "output_index": 0,
               "item_id": "m1", "content_index": 0, "delta": "hi"}

    client = LiteLLMClient.__new__(LiteLLMClient)
    with pytest.raises(LLMProviderFailure):
        await client._accumulate_responses_stream_native(stream())


async def test_subscription_incomplete_event_is_provider_failure(monkeypatch):
    from redteam.core.ports.llm import LLMProviderFailure

    async def stream():
        yield {"type": "response.incomplete"}

    _SubscriptionHarness(monkeypatch, stream())
    with pytest.raises(LLMProviderFailure):
        await LiteLLMClient("chatgpt/unit-model").complete_native(
            [{"role": "user", "content": "hi"}], [SPEC]
        )


async def test_fake_native_roundtrip():
    call = NativeToolCall(call_id="call_f", name="lookup", arguments={"q": 1})
    fake = FakeLLMClient(canned_native_text="note", canned_native_calls=[call])
    result = await fake.complete_native([{"role": "user", "content": "hi"}], [SPEC])
    assert result.text == "note"
    assert result.tool_calls == (call,)
    assert result.protocol_error is None
    assert fake.last_native_request is not None
    assert fake.last_native_request["tools"] == [SPEC]
