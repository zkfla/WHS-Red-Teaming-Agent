"""Structured chat without native schema enforcement carries the schema in the prompt.

SDK boundary only: a stubbed `litellm` module records what would be sent on the wire.
"""

from __future__ import annotations

import copy
import json
import sys
from types import SimpleNamespace

import pytest

from redteam.llm.litellm import LiteLLMClient, _schema_json_system

SCHEMA = {"type": "object", "properties": {"a": {"type": "integer"}}, "required": ["a"]}
INSTRUCTION = (
    "다음 JSON Schema에 맞는 JSON 객체 하나만 응답하세요. "
    "설명문이나 Markdown 코드 펜스는 포함하지 마세요: " + json.dumps(SCHEMA)
)


def _stub_sdk(monkeypatch, calls):
    async def _acompletion(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content='{"a": 1}'))],
            usage=SimpleNamespace(total_tokens=42),
        )

    stub = SimpleNamespace(
        get_llm_provider=lambda model: (model, "openai", None, None),
        acompletion=_acompletion,
    )
    monkeypatch.setitem(sys.modules, "litellm", stub)
    monkeypatch.setitem(sys.modules, "litellm.types.utils", SimpleNamespace(
        LlmProviders=lambda value: value,
    ))
    monkeypatch.setitem(sys.modules, "litellm.utils", SimpleNamespace(
        ProviderConfigManager=SimpleNamespace(get_provider_chat_config=lambda **kwargs: None),
    ))


def test_instruction_text_is_the_shared_trusted_schema_prompt():
    assert _schema_json_system(None, SCHEMA) == INSTRUCTION
    assert _schema_json_system("sys", SCHEMA) == "sys\n\n" + INSTRUCTION


@pytest.mark.asyncio
async def test_json_object_fallback_sends_schema_and_keeps_inputs_usage_and_effort(monkeypatch):
    calls = []
    _stub_sdk(monkeypatch, calls)
    client = LiteLLMClient("unit-model", reasoning_effort="low")
    messages = [{"role": "user", "content": "go"}]
    snapshot = copy.deepcopy(messages)

    result = await client.complete_structured_with_usage(
        messages=messages, response_schema=SCHEMA, system="sys",
    )

    kwargs = calls[0]
    assert kwargs["response_format"] == {"type": "json_object"}
    assert kwargs["reasoning_effort"] == "low"
    assert kwargs["messages"][0] == {"role": "system", "content": "sys\n\n" + INSTRUCTION}
    assert kwargs["messages"][1:] == snapshot
    assert messages == snapshot
    assert result.value == '{"a": 1}'
    assert result.total_tokens == 42


@pytest.mark.asyncio
async def test_fallback_without_caller_system_sends_only_the_schema_system_message(monkeypatch):
    calls = []
    _stub_sdk(monkeypatch, calls)
    client = LiteLLMClient("unit-model")

    await client.complete_structured_with_usage(
        messages=[{"role": "user", "content": "go"}], response_schema=SCHEMA,
    )

    assert calls[0]["messages"] == [
        {"role": "system", "content": INSTRUCTION},
        {"role": "user", "content": "go"},
    ]


@pytest.mark.asyncio
async def test_json_only_wrapper_gets_the_same_fallback_schema(monkeypatch):
    calls = []
    _stub_sdk(monkeypatch, calls)
    client = LiteLLMClient("unit-model")

    parsed = await client.complete_structured(
        messages=[{"role": "user", "content": "go"}], response_schema=SCHEMA,
    )

    assert parsed == {"a": 1}
    assert calls[0]["messages"][0] == {"role": "system", "content": INSTRUCTION}


@pytest.mark.asyncio
async def test_native_schema_route_is_unchanged(monkeypatch):
    calls = []
    _stub_sdk(monkeypatch, calls)
    client = LiteLLMClient("openai/unit-model")
    messages = [{"role": "user", "content": "go"}]

    await client.complete_structured_with_usage(
        messages=messages, response_schema=SCHEMA, system="sys",
    )

    kwargs = calls[0]
    assert kwargs["response_format"]["type"] == "json_schema"
    assert kwargs["response_format"]["json_schema"]["schema"] == SCHEMA
    assert kwargs["messages"] == [{"role": "system", "content": "sys"}, *messages]


@pytest.mark.asyncio
async def test_caller_native_response_format_is_passed_without_prompt_change(monkeypatch):
    calls = []
    _stub_sdk(monkeypatch, calls)
    client = LiteLLMClient("unit-model")
    native = {"type": "json_schema", "json_schema": {"name": "x", "schema": SCHEMA}}

    await client.complete_structured_with_usage(
        messages=[{"role": "user", "content": "go"}], response_schema=SCHEMA,
        system="sys", response_format=native,
    )

    kwargs = calls[0]
    assert kwargs["response_format"] == native
    assert kwargs["messages"][0] == {"role": "system", "content": "sys"}


@pytest.mark.asyncio
async def test_caller_json_object_format_still_gets_the_schema_prompt(monkeypatch):
    calls = []
    _stub_sdk(monkeypatch, calls)
    client = LiteLLMClient("unit-model")

    await client.complete_structured_with_usage(
        messages=[{"role": "user", "content": "go"}], response_schema=SCHEMA,
        system="sys", response_format={"type": "json_object"},
    )

    kwargs = calls[0]
    assert kwargs["response_format"] == {"type": "json_object"}
    assert kwargs["messages"][0] == {"role": "system", "content": "sys\n\n" + INSTRUCTION}
