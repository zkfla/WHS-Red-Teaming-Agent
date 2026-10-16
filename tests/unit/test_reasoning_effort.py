"""Reasoning effort: wire keys, merge-not-reset, identity, continuity.

Wire-SDK mocks only (stubbed ``litellm`` module or the real installed SDK
with its network boundary patched); no live provider calls, no real keys.
"""

from __future__ import annotations

import copy
import os
import sys
from types import SimpleNamespace

import pytest

from redteam.core.config import LLMConfig
from redteam.llm import reasoning_effort as effort_mod
from redteam.llm.litellm import (
    LiteLLMClient,
    _encrypted_continuation,
    _encrypted_reasoning_items,
    _replay_chat_message,
    _responses_input_from_chat,
)
from redteam.llm.opencode_go_config import (
    llm_connection_fingerprint_for_config as go_fingerprint,
)

SCHEMA = {"type": "object", "properties": {"a": {"type": "integer"}}}
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")


def _stub_sdk(monkeypatch, calls, provider="openai", drop_params=False):
    async def _acompletion(**kwargs):
        calls.append(("acompletion", kwargs))
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(
                content='{"a": 1}',
                tool_calls=[SimpleNamespace(
                    id="c1",
                    function=SimpleNamespace(name="tool_a", arguments='{"a": 1}'),
                )],
            ))],
            usage=None,
        )

    async def _aresponses(**kwargs):
        calls.append(("aresponses", kwargs))
        return SimpleNamespace(
            status="completed",
            output=[SimpleNamespace(
                type="message",
                content=[SimpleNamespace(type="output_text", text='{"a": 1}')],
            )],
            usage=None,
        )

    stub = SimpleNamespace(
        get_llm_provider=lambda model: (model, provider, None, None),
        acompletion=_acompletion,
        aresponses=_aresponses,
        model_cost={},
        register_model=lambda models: None,
    )
    if drop_params:
        stub.drop_params = True
    monkeypatch.setitem(sys.modules, "litellm", stub)
    monkeypatch.setitem(sys.modules, "litellm.types.utils", SimpleNamespace(
        LlmProviders=lambda value: value,
    ))
    monkeypatch.setitem(sys.modules, "litellm.utils", SimpleNamespace(
        ProviderConfigManager=SimpleNamespace(get_provider_chat_config=lambda **kwargs: None),
    ))
    return stub


def test_default_kwargs_unset_is_empty():
    assert effort_mod.litellm_default_kwargs(None) == {}
    assert effort_mod.litellm_default_kwargs("  ") == {}


@pytest.mark.parametrize(
    "level", ["none", "minimal", "low", "medium", "high", "xhigh", "max"],
)
def test_default_kwargs_forward_top_level_key(level):
    assert effort_mod.litellm_default_kwargs(level) == {"reasoning_effort": level}
    assert "drop_params" not in effort_mod.litellm_default_kwargs(level)


def test_default_kwargs_rejects_unknown_loudly():
    with pytest.raises(ValueError, match="REDTEAM_LLM_REASONING_EFFORT"):
        effort_mod.litellm_default_kwargs("ultra")


def test_output_config_effort_map_minimal_rides_low():
    assert effort_mod.OUTPUT_CONFIG_EFFORTS["minimal"] == "low"
    assert effort_mod.OUTPUT_CONFIG_EFFORTS["high"] == "high"
    assert effort_mod.OUTPUT_CONFIG_EFFORTS["max"] == "max"


def test_apply_messages_effort_merges_format_and_caller_fields():
    existing = {"custom": 1, "format": {"type": "old"}}
    merged = effort_mod.apply_messages_effort(existing, "high")
    assert merged["custom"] == 1
    assert merged["format"] == {"type": "old"}
    assert merged["effort"] == "high"
    assert existing == {"custom": 1, "format": {"type": "old"}}
    assert effort_mod.apply_messages_effort(existing, "minimal")["effort"] == "low"


def test_apply_messages_effort_none_sets_no_key():
    assert effort_mod.apply_messages_effort(None, None) == {}
    kept = effort_mod.apply_messages_effort({"format": {"type": "x"}}, "none")
    assert kept == {"format": {"type": "x"}}
    assert "effort" not in effort_mod.apply_messages_effort(None, "none")


def test_apply_messages_effort_rejects_unknown():
    with pytest.raises(ValueError, match="REDTEAM_LLM_REASONING_EFFORT"):
        effort_mod.apply_messages_effort(None, "ultra")


def test_shared_preflight_litellm_unset_stays_none():
    config = LLMConfig(provider="litellm", default_model="m")
    assert effort_mod.litellm_connection_fingerprint_for_config(config) is None
    assert effort_mod.llm_connection_fingerprint(config) is None


def test_shared_preflight_litellm_effort_change_alters_binding():
    high = LLMConfig(provider="litellm", default_model="m", reasoning_effort="high")
    low = LLMConfig(provider="litellm", default_model="m", reasoning_effort="low")
    unset = LLMConfig(provider="litellm", default_model="m")
    assert effort_mod.llm_connection_fingerprint(high) is not None
    assert effort_mod.llm_connection_fingerprint(high) == (
        effort_mod.litellm_connection_fingerprint_for_config(high)
    )
    assert effort_mod.llm_connection_fingerprint(high) != (
        effort_mod.llm_connection_fingerprint(low)
    )
    assert effort_mod.llm_connection_fingerprint(unset) is None


def test_shared_preflight_litellm_cyber_program_alters_binding():
    blue = LLMConfig(
        provider="litellm", default_model="chatgpt/m", cyber_access_program="daybreak_blue"
    )
    standard = blue.model_copy(update={"cyber_access_program": "standard"})
    assert effort_mod.llm_connection_fingerprint(blue) is not None
    assert effort_mod.llm_connection_fingerprint(blue) != (
        effort_mod.llm_connection_fingerprint(standard)
    )


def test_llm_config_reads_and_validates_cyber_program_environment():
    config = LLMConfig.from_environment({
        "REDTEAM_LLM_PROVIDER": "litellm",
        "REDTEAM_LLM_MODEL": "chatgpt/m",
        "REDTEAM_LLM_CYBER_ACCESS_PROGRAM": "DAYBREAK_BLUE",
    })
    assert config.cyber_access_program == "daybreak_blue"
    with pytest.raises(ValueError, match="REDTEAM_LLM_CYBER_ACCESS_PROGRAM"):
        LLMConfig.from_environment({
            "REDTEAM_LLM_PROVIDER": "litellm",
            "REDTEAM_LLM_CYBER_ACCESS_PROGRAM": "blue",
        })


def test_shared_preflight_go_and_other_providers():
    go = LLMConfig(
        provider="opencode-go",
        default_model="openai/model-a",
        api_key_env_var="TEST_GO_KEY",
    )
    assert effort_mod.llm_connection_fingerprint(go) == go_fingerprint(go)
    assert effort_mod.llm_connection_fingerprint(go) is not None
    go_high = LLMConfig(
        provider="opencode-go",
        default_model="openai/model-a",
        api_key_env_var="TEST_GO_KEY",
        reasoning_effort="high",
    )
    assert effort_mod.llm_connection_fingerprint(go_high) != (
        effort_mod.llm_connection_fingerprint(go)
    )
    assert effort_mod.llm_connection_fingerprint(SimpleNamespace(provider="watson")) is None


def test_go_unset_hash_matches_legacy_record():
    import hashlib
    import json

    from redteam.llm.opencode_go_config import USER_AGENT

    go = LLMConfig(
        provider="opencode-go",
        default_model="openai/model-a",
        api_key_env_var="TEST_GO_KEY",
    )
    legacy = hashlib.sha256(
        json.dumps(
            {
                "v": "v1",
                "endpoint": "https://opencode.ai/zen/go/v1",
                "protocol": "openai/",
                "model": "openai/model-a",
                "credential": "TEST_GO_KEY",
                "ua": USER_AGENT,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    assert go_fingerprint(go) == legacy
    assert "reasoning_effort" not in go.model_dump()


@pytest.mark.asyncio
async def test_chat_complete_forwards_effort_and_preserves_messages(monkeypatch):
    calls = []
    _stub_sdk(monkeypatch, calls)
    thinking_block = {"type": "thinking", "thinking": "", "signature": "sig-1"}
    messages = [
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": [thinking_block, {"type": "text", "text": "hi"}]},
    ]
    snapshot = copy.deepcopy(messages)
    client = LiteLLMClient("unit-model", reasoning_effort="high")
    await client.complete(messages=messages, system="sys")
    assert len(calls) == 1
    _, kwargs = calls[0]
    assert kwargs["reasoning_effort"] == "high"
    assert "drop_params" not in kwargs
    sent = kwargs["messages"]
    assert sent[0] == {"role": "system", "content": "sys"}
    assert sent[1:] == snapshot
    assert messages == snapshot


@pytest.mark.asyncio
async def test_chat_complete_unset_sends_historical_keys(monkeypatch):
    calls = []
    _stub_sdk(monkeypatch, calls)
    client = LiteLLMClient("unit-model")
    assert client.connection_fingerprint is None
    await client.complete(messages=[{"role": "user", "content": "go"}])
    _, kwargs = calls[0]
    assert "reasoning_effort" not in kwargs
    assert "drop_params" not in kwargs


@pytest.mark.asyncio
async def test_structured_chat_forwards_effort(monkeypatch):
    calls = []
    _stub_sdk(monkeypatch, calls)
    client = LiteLLMClient("unit-model", reasoning_effort="low")
    result = await client.complete_structured_with_usage(
        messages=[{"role": "user", "content": "go"}], response_schema=SCHEMA
    )
    assert result.total_tokens is None
    _, kwargs = calls[0]
    assert kwargs["reasoning_effort"] == "low"
    assert kwargs["response_format"]["type"] == "json_object"


@pytest.mark.asyncio
async def test_responses_structured_forwards_effort(monkeypatch):
    calls = []
    _stub_sdk(monkeypatch, calls)
    client = LiteLLMClient("unit-model", use_responses_api=True, reasoning_effort="medium")
    result = await client.complete_structured_with_usage(
        messages=[{"role": "user", "content": "go"}], response_schema=SCHEMA
    )
    assert result.value == '{"a": 1}'
    _, kwargs = calls[0]
    assert kwargs["reasoning_effort"] == "medium"
    assert kwargs["text"]["format"]["type"] == "json_schema"


@pytest.mark.asyncio
async def test_chatgpt_subscription_forwards_pinned_cyber_program(monkeypatch):
    calls = []
    _stub_sdk(monkeypatch, calls, provider="chatgpt")
    client = LiteLLMClient(
        "chatgpt/unit-model", cyber_access_program="daybreak_blue",
        extra_body={"existing": True},
    )
    await client.complete_with_usage(
        messages=[{"role": "user", "content": "go"}], stream=False
    )
    _, kwargs = calls[0]
    assert kwargs["extra_body"] == {
        "existing": True,
        "access_programs": {"cyber": "daybreak_blue"},
    }


def test_cyber_program_rejects_non_chatgpt_provider(monkeypatch):
    calls = []
    _stub_sdk(monkeypatch, calls, provider="openai")
    with pytest.raises(ValueError, match="chatgpt/"):
        LiteLLMClient("openai/unit-model", cyber_access_program="daybreak_blue")


@pytest.mark.asyncio
async def test_native_chat_preserves_thinking_and_tool_calls(monkeypatch):
    from redteam.core.ports.llm import NativeToolSpec

    calls = []
    _stub_sdk(monkeypatch, calls)
    thinking_block = {"type": "thinking", "thinking": "", "signature": "sig-9"}
    tool_call = {"id": "c1", "function": {"name": "tool_a", "arguments": '{"a": 1}'}}
    messages = [
        {"role": "assistant", "content": [thinking_block], "tool_calls": [tool_call]},
        {"role": "tool", "tool_call_id": "c1", "content": "done"},
    ]
    snapshot = copy.deepcopy(messages)
    client = LiteLLMClient("unit-model", reasoning_effort="high")
    completion = await client.complete_native(messages, [NativeToolSpec(name="tool_a")])
    assert [call.call_id for call in completion.tool_calls] == ["c1"]
    _, kwargs = calls[0]
    assert kwargs["reasoning_effort"] == "high"
    assert kwargs["messages"] == snapshot


def test_constructor_rejects_invalid_effort(monkeypatch):
    calls = []
    _stub_sdk(monkeypatch, calls)
    with pytest.raises(ValueError, match="REDTEAM_LLM_REASONING_EFFORT"):
        LiteLLMClient("unit-model", reasoning_effort="ultra")
    assert calls == []


def test_constructor_rejects_drop_params_with_effort(monkeypatch):
    calls = []
    _stub_sdk(monkeypatch, calls)
    with pytest.raises(ValueError, match="drop_params"):
        LiteLLMClient("unit-model", reasoning_effort="high", drop_params=True)
    assert calls == []


def test_constructor_rejects_global_drop_params_with_effort(monkeypatch):
    calls = []
    _stub_sdk(monkeypatch, calls, drop_params=True)
    with pytest.raises(ValueError, match="drop_params"):
        LiteLLMClient("unit-model", reasoning_effort="high")


@pytest.mark.asyncio
async def test_percall_drop_params_with_effort_raises_before_sdk(monkeypatch):
    async def _explode(**kwargs):
        raise AssertionError("SDK must not be reached")

    monkeypatch.setitem(
        sys.modules,
        "litellm",
        SimpleNamespace(
            get_llm_provider=lambda model: (model, "openai", None, None),
            acompletion=_explode,
        ),
    )
    client = LiteLLMClient("unit-model", reasoning_effort="high")
    with pytest.raises(ValueError, match="drop_params"):
        await client.complete(messages=[{"role": "user", "content": "go"}], drop_params=True)


def test_client_fingerprint_matches_shared_preflight(monkeypatch):
    calls = []
    _stub_sdk(monkeypatch, calls)
    client = LiteLLMClient("unit-model", api_key_env_var="LLM_API_KEY", reasoning_effort="high")
    expected = effort_mod.litellm_connection_fingerprint_for_config(
        LLMConfig(provider="litellm", default_model="unit-model", reasoning_effort="high")
    )
    assert client.connection_fingerprint == expected
    assert client.connection_fingerprint is not None


def _go_client(monkeypatch, model, effort):
    litellm = pytest.importorskip("litellm")

    from redteam.llm.opencode_go import build as go_build

    monkeypatch.setenv("TEST_GO_KEY", "sk-go-fixture")
    calls = []

    async def _acompletion(**kwargs):
        calls.append(("acompletion", kwargs))
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content='{"a": 1}', tool_calls=None))],
            usage=None,
        )

    async def _aresponses(**kwargs):
        calls.append(("aresponses", kwargs))
        return SimpleNamespace(
            status="completed",
            output=[SimpleNamespace(
                type="message",
                content=[SimpleNamespace(type="output_text", text='{"a": 1}')],
            )],
            usage=None,
        )

    monkeypatch.setattr(litellm, "acompletion", _acompletion)
    monkeypatch.setattr(litellm, "aresponses", _aresponses)
    config = LLMConfig(
        provider="opencode-go",
        default_model=model,
        api_key_env_var="TEST_GO_KEY",
        reasoning_effort=effort,
    )
    return go_build(config), calls


@pytest.mark.asyncio
async def test_go_chat_threads_effort(monkeypatch):
    client, calls = _go_client(monkeypatch, "openai/model-a", "high")
    assert client.protocol == "chat"
    await client.complete(messages=[{"role": "user", "content": "go"}])
    _, kwargs = calls[0]
    assert kwargs["reasoning_effort"] == "high"
    assert kwargs["model"] == "openai/model-a"
    assert "drop_params" not in kwargs


@pytest.mark.asyncio
async def test_go_responses_threads_effort(monkeypatch):
    client, calls = _go_client(monkeypatch, "openai/responses/model-a", "medium")
    assert client.protocol == "responses"
    result = await client.complete_structured_with_usage(
        messages=[{"role": "user", "content": "go"}], response_schema=SCHEMA
    )
    assert result.value == '{"a": 1}'
    kinds = [kind for kind, _ in calls]
    assert kinds == ["aresponses"]
    _, kwargs = calls[0]
    assert kwargs["reasoning_effort"] == "medium"
    assert kwargs["text"]["format"]["type"] == "json_schema"


@pytest.mark.asyncio
async def test_go_messages_structured_merges_format_and_effort(monkeypatch):
    client, calls = _go_client(monkeypatch, "anthropic/cl-x", "high")
    assert client.protocol == "messages"
    structured = client._structured_kwargs({"output_config": {"custom": 1}}, SCHEMA)
    assert structured["output_config"]["custom"] == 1
    assert structured["output_config"]["format"]["type"] == "json_schema"
    assert structured["output_config"]["format"]["schema"] == SCHEMA
    assert structured["output_config"]["effort"] == "high"
    result = await client.complete_structured_with_usage(
        messages=[{"role": "user", "content": "go"}], response_schema=SCHEMA
    )
    assert result.value == '{"a": 1}'
    _, kwargs = calls[0]
    assert kwargs["output_config"]["format"]["type"] == "json_schema"
    assert kwargs["output_config"]["effort"] == "high"
    assert kwargs["reasoning_effort"] == "high"
    assert "authorization" in kwargs["extra_headers"]


@pytest.mark.asyncio
async def test_go_messages_unset_effort_keeps_historical_shape(monkeypatch):
    client, calls = _go_client(monkeypatch, "anthropic/cl-x", None)
    structured = client._structured_kwargs({}, SCHEMA)
    assert structured["output_config"] == {"format": {"type": "json_schema", "schema": SCHEMA}}
    assert "effort" not in structured["output_config"]
    await client.complete(messages=[{"role": "user", "content": "go"}])
    assert "reasoning_effort" not in calls[0][1]
    assert "output_config" not in calls[0][1]


def test_go_http_body_has_format_and_effort(monkeypatch):
    litellm = pytest.importorskip("litellm")
    from litellm.llms.anthropic.chat.transformation import AnthropicConfig

    monkeypatch.setattr(litellm, "drop_params", False)
    output_config = effort_mod.apply_messages_effort(
        {"format": {"type": "json_schema", "schema": SCHEMA}}, "high"
    )
    body = AnthropicConfig().transform_request(
        model="cl-x",
        messages=[{"role": "user", "content": "hi"}],
        optional_params={"max_tokens": 100, "output_config": output_config},
        litellm_params={},
        headers={},
    )
    assert body["output_config"]["format"] == {"type": "json_schema", "schema": SCHEMA}
    assert body["output_config"]["effort"] == "high"


def test_go_http_body_minimal_maps_low(monkeypatch):
    litellm = pytest.importorskip("litellm")
    from litellm.llms.anthropic.chat.transformation import AnthropicConfig

    monkeypatch.setattr(litellm, "drop_params", False)
    output_config = effort_mod.apply_messages_effort(None, "minimal")
    body = AnthropicConfig().transform_request(
        model="cl-x",
        messages=[{"role": "user", "content": "hi"}],
        optional_params={"max_tokens": 100, "output_config": output_config},
        litellm_params={},
        headers={},
    )
    assert body["output_config"]["effort"] == "low"
    assert "format" not in body["output_config"]


def test_build_llm_client_threads_litellm_effort(monkeypatch):
    import types as _types

    from redteam.app.bootstrap import build_llm_client

    module = _types.ModuleType("redteam.llm.litellm")
    calls = {}

    class _Client:
        def __init__(self, model=None, api_key=None, **kwargs):
            calls.update(model=model, api_key=api_key, kwargs=kwargs)

    module.LiteLLMClient = _Client
    monkeypatch.setitem(sys.modules, "redteam.llm.litellm", module)
    monkeypatch.setenv("LLM_API_KEY", "sk-litellm")
    build_llm_client(LLMConfig(
        provider="litellm", default_model="m", reasoning_effort="high"
    ))
    assert calls["model"] == "m"
    assert calls["kwargs"]["reasoning_effort"] == "high"
    assert calls["kwargs"]["api_key_env_var"] == "LLM_API_KEY"
    build_llm_client(LLMConfig(provider="litellm", default_model="m"))
    assert calls["kwargs"]["reasoning_effort"] is None
    assert calls["kwargs"]["cyber_access_program"] is None


def test_build_llm_client_threads_litellm_cyber_program(monkeypatch):
    import types as _types

    from redteam.app.bootstrap import build_llm_client

    module = _types.ModuleType("redteam.llm.litellm")
    calls = {}

    class _Client:
        def __init__(self, model=None, api_key=None, **kwargs):
            calls.update(model=model, api_key=api_key, kwargs=kwargs)

    module.LiteLLMClient = _Client
    monkeypatch.setitem(sys.modules, "redteam.llm.litellm", module)
    build_llm_client(LLMConfig(
        provider="litellm",
        default_model="chatgpt/m",
        cyber_access_program="daybreak_blue",
    ))
    assert calls["kwargs"]["cyber_access_program"] == "daybreak_blue"


@pytest.mark.asyncio
async def test_percall_conflicting_effort_rejected_before_sdk(monkeypatch):
    calls = []
    _stub_sdk(monkeypatch, calls)
    client = LiteLLMClient("unit-model", reasoning_effort="high")
    with pytest.raises(ValueError, match="conflicts with the pinned"):
        await client.complete(
            messages=[{"role": "user", "content": "go"}], reasoning_effort="low"
        )
    assert calls == []


@pytest.mark.asyncio
async def test_percall_same_effort_passes_through(monkeypatch):
    calls = []
    _stub_sdk(monkeypatch, calls)
    client = LiteLLMClient("unit-model", reasoning_effort="high")
    await client.complete(messages=[{"role": "user", "content": "go"}], reasoning_effort="high")
    assert calls[0][1]["reasoning_effort"] == "high"


@pytest.mark.asyncio
async def test_percall_effort_normalized_for_unpinned_config(monkeypatch):
    calls = []
    _stub_sdk(monkeypatch, calls)
    client = LiteLLMClient("unit-model")
    await client.complete(messages=[{"role": "user", "content": "go"}], reasoning_effort="HIGH")
    assert calls[0][1]["reasoning_effort"] == "high"


@pytest.mark.asyncio
async def test_percall_none_against_pinned_effort_rejected(monkeypatch):
    calls = []
    _stub_sdk(monkeypatch, calls)
    client = LiteLLMClient("unit-model", reasoning_effort="high")
    with pytest.raises(ValueError, match="conflicts with the pinned"):
        await client.complete(messages=[{"role": "user", "content": "go"}], reasoning_effort=None)
    assert calls == []


@pytest.mark.asyncio
async def test_global_drop_toggle_after_build_checked_per_request(monkeypatch):
    calls = []
    stub = _stub_sdk(monkeypatch, calls)
    client = LiteLLMClient("unit-model", reasoning_effort="high")
    await client.complete(messages=[{"role": "user", "content": "go"}])
    assert len(calls) == 1
    stub.drop_params = True
    with pytest.raises(ValueError, match="drop_params"):
        await client.complete(messages=[{"role": "user", "content": "go"}])
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_global_drop_toggle_blocks_go_request(monkeypatch):
    litellm = pytest.importorskip("litellm")

    client, calls = _go_client(monkeypatch, "openai/model-a", "high")
    await client.complete(messages=[{"role": "user", "content": "go"}])
    assert len(calls) == 1
    monkeypatch.setattr(litellm, "drop_params", True)
    with pytest.raises(ValueError, match="drop_params"):
        await client.complete(messages=[{"role": "user", "content": "go"}])
    assert len(calls) == 1


@pytest.mark.parametrize("block", [
    {"type": "thinking", "thinking": "private", "signature": "cipher"},
    {"type": "thinking", "thinking": " ", "signature": "cipher"},
    {"type": "thinking", "thinking": "", "signature": "cipher", "text": "private"},
    {"type": "thinking", "thinking": "", "data": "cipher"},
    {"type": "redacted_thinking", "signature": "cipher"},
])
def test_native_capture_and_replay_reject_plaintext_or_malformed(block):
    with pytest.raises(ValueError):
        _encrypted_continuation({"thinking_blocks": [block]})
    with pytest.raises(ValueError):
        _replay_chat_message({"role": "assistant", "provider_continuation": [block]})


def test_redacted_ciphertext_round_trip_and_empty_marker_removal():
    block = {"type": "redacted_thinking", "data": "cipher"}
    assert _encrypted_continuation({"thinking_blocks": [block]}) == (block,)
    replay = _replay_chat_message({"role": "assistant", "provider_continuation": [block]})
    assert replay["thinking_blocks"] == [block]
    assert "provider_continuation" not in replay
    assert _replay_chat_message({"role": "assistant", "provider_continuation": []}) == {
        "role": "assistant"
    }


def test_responses_project_encrypted_state_and_reject_plaintext_replay():
    original = {"type": "reasoning", "id": "r1", "encrypted_content": "cipher",
                "summary": [{"type": "summary_text", "text": "private"}], "text": "private"}
    projected = _encrypted_reasoning_items([original], required=True)
    assert projected == ({"type": "reasoning", "id": "r1",
                          "encrypted_content": "cipher", "summary": []},)
    with pytest.raises(ValueError):
        _responses_input_from_chat([{"role": "assistant", "provider_continuation": [original]}])
    with pytest.raises(ValueError):
        _encrypted_reasoning_items([{"type": "reasoning", "summary": []}], required=True)


async def test_responses_stream_captures_only_completed_encrypted_state(monkeypatch):
    _stub_sdk(monkeypatch, [])
    client = LiteLLMClient("unit-model", reasoning_effort="high", use_responses_api=True)
    encrypted = {"type": "reasoning", "id": "r1", "encrypted_content": "cipher",
                 "summary": []}
    async def stream():
        yield {"type": "response.output_item.done", "item": encrypted}
        yield {"type": "response.output_text.delta", "delta": "done"}
        yield {"type": "response.completed", "response": {"output": [encrypted]}}
    completion = await client._accumulate_responses_stream_native(stream())
    assert completion.continuation == (encrypted,)
    assert completion.text == "done"
