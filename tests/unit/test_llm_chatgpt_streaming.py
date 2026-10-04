"""Subscription streaming must survive LiteLLM's model-catalog lookup."""

from __future__ import annotations

import pytest

from redteam.llm.litellm import LiteLLMClient


def test_unlisted_subscription_model_keeps_native_streaming(monkeypatch):
    litellm = pytest.importorskip("litellm")
    transformation = pytest.importorskip("litellm.llms.chatgpt.responses.transformation")
    model = "chatgpt/unit-unlisted-stream-model"

    def resolve(model, custom_llm_provider=None, **kwargs):
        # Provider discovery is independent of OAuth authentication here.
        return model.split("/", 1)[-1], "chatgpt", None, None

    monkeypatch.setattr(litellm, "get_llm_provider", resolve)
    monkeypatch.setattr(
        litellm,
        "model_cost",
        {
            "unit-new-openai-model": {
                "litellm_provider": "openai",
                "mode": "chat",
                "supports_reasoning": True,
                "max_input_tokens": 10000,
                "input_cost_per_token": 0.001,
            },
            "unit-embedding-model": {"litellm_provider": "openai", "mode": "embedding"},
            "unit-other-provider": {"litellm_provider": "anthropic", "mode": "chat"},
        },
    )
    monkeypatch.setattr(
        litellm.utils, "_runtime_registered_model_cost", {}, raising=False
    )

    LiteLLMClient(model)

    provider = transformation.ChatGPTResponsesAPIConfig()
    # This SDK decision controls whether the HTTP handler removes `stream`
    # from the wire request. No network call or login is needed.
    assert not provider.should_fake_stream(
        model=model.split("/", 1)[-1],
        stream=True,
        custom_llm_provider="chatgpt",
    )
    # A future SDK catalog entry is picked up without editing a local list;
    # unrelated providers/modalities and API billing rates are not copied.
    alias = litellm.model_cost["chatgpt/unit-new-openai-model"]
    assert alias["supports_reasoning"] is True
    assert alias["max_input_tokens"] == 10000
    assert "input_cost_per_token" not in alias
    assert "chatgpt/unit-embedding-model" not in litellm.model_cost
    assert "chatgpt/unit-other-provider" not in litellm.model_cost
