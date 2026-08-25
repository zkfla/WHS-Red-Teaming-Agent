"""llm/anthropic_.py, llm/openai_.py 의 LLMError 변환 + trace_hook 단위 테스트.

실제 네트워크 호출 없이, 벤더 클라이언트의 `.messages.create`/
`.chat.completions.create` 만 몽키패치해서 검증한다 — 클라이언트 생성 자체는
네트워크를 타지 않는다.
"""
from __future__ import annotations

import httpx
import pytest
from pydantic import BaseModel

from finredteam.llm.anthropic_ import AnthropicClient
from finredteam.llm.base import LLMError, LLMEventKind
from finredteam.llm.openai_ import OpenAIClient


class _Step(BaseModel):
    thought: str
    action: str


def _fake_request() -> httpx.Request:
    return httpx.Request("POST", "http://test.invalid/v1/messages")


# ── Anthropic ────────────────────────────────────────────────────────────


def test_anthropic_api_error_becomes_llm_error_not_swallowed():
    import anthropic

    client = AnthropicClient(model="claude-x", api_key="sk-test")

    def _raise(*args, **kwargs):
        raise anthropic.APITimeoutError(request=_fake_request())

    client._client.messages.create = _raise  # type: ignore[method-assign]

    with pytest.raises(LLMError):
        client.complete_json("system", "user", _Step)


def test_anthropic_non_api_error_is_not_wrapped_as_llm_error():
    """★ ② — 코드 버그(여기선 그냥 임의의 RuntimeError)는 LLMError 로 위장되지
    않고 원래 타입 그대로 전파되어야 한다."""
    client = AnthropicClient(model="claude-x", api_key="sk-test")

    def _raise(*args, **kwargs):
        raise RuntimeError("이건 우리 코드 버그라고 가정한다")

    client._client.messages.create = _raise  # type: ignore[method-assign]

    with pytest.raises(RuntimeError):
        client.complete_json("system", "user", _Step)


def test_anthropic_trace_hook_emits_started_and_finished_on_error():
    import anthropic

    events = []
    client = AnthropicClient(model="claude-x", api_key="sk-test", trace_hook=events.append)

    def _raise(*args, **kwargs):
        raise anthropic.APIConnectionError(request=_fake_request())

    client._client.messages.create = _raise  # type: ignore[method-assign]

    with pytest.raises(LLMError):
        client.complete_json("system", "user", _Step)

    kinds = [e.kind for e in events]
    assert kinds == [LLMEventKind.CALL_STARTED, LLMEventKind.CALL_FINISHED]
    assert events[-1].error is not None


# ── OpenAI ───────────────────────────────────────────────────────────────


def test_openai_api_error_becomes_llm_error():
    import openai

    client = OpenAIClient(model="gpt-x", api_key="sk-test")

    def _raise(*args, **kwargs):
        raise openai.APITimeoutError(request=_fake_request())

    client._client.chat.completions.create = _raise  # type: ignore[method-assign]

    with pytest.raises(LLMError):
        client.complete_json("system", "user", _Step)


def test_openai_non_api_error_is_not_wrapped():
    client = OpenAIClient(model="gpt-x", api_key="sk-test")

    def _raise(*args, **kwargs):
        raise RuntimeError("코드 버그라고 가정")

    client._client.chat.completions.create = _raise  # type: ignore[method-assign]

    with pytest.raises(RuntimeError):
        client.complete_json("system", "user", _Step)
