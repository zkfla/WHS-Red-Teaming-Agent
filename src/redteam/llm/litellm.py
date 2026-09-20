"""``LLMClient`` backed by LiteLLM.

Works on this project's Python 3.10+ floor — verified directly
(``pip install -e ".[llm]"`` then ``import litellm`` on 3.10.4): current
``litellm`` releases satisfying ``litellm>=1.50`` do not require 3.11+.
See ``docs/adr/0002-litellm-gateway.md`` for history (an earlier
``litellm`` release did require 3.11's ``typing.NotRequired``; that is no
longer the case).

``litellm`` is still imported lazily inside the methods, not at module
scope: it is a genuinely optional dependency (``pip install -e ".[llm]"``)
with a large transitive dependency footprint (``boto3``, ``openai``,
``tiktoken``, ``tokenizers``, ...) that the base install and
``FakeLLMClient`` should not force on every install.

``chatgpt/<model>`` (ChatGPT Plus/Pro subscription via OAuth device flow,
no API key) routes through a different code path than every other
provider here. Provider identity is resolved once via litellm's own
``litellm.get_llm_provider(model)`` (the same resolver litellm uses
internally to route every call) rather than a locally duplicated
``"chatgpt/"`` prefix check — if litellm ever renames or reorganizes the
provider, this class tracks it automatically instead of silently
misrouting. Verified directly against a real ``chatgpt/gpt-5.5``
account: litellm's normal ``acompletion()`` bridges this provider through
an internal Responses-API transformation that can raise
``ValueError("Unknown items in responses API response: []")`` /
surface as ``APIConnectionError`` for a short, non-agentic prompt like
ours (that bridge assumes the long-running "Codex CLI coding agent"
shape this backend was built for) — calling the native
``litellm.aresponses()`` streaming API directly instead avoids that
bridge entirely and returns real content reliably (tested with plain
text and with ``text={"format": {"type": "json_object"}}``). Every other
provider (``openai/...``, ``anthropic/...``, etc.) keeps using the plain
``acompletion()`` path below, unchanged — this workaround is scoped to
the ``chatgpt`` provider specifically, not a blanket switch to the
Responses API.
"""

from __future__ import annotations

import json
from typing import Any

_CHATGPT_PROVIDER = "chatgpt"


class LiteLLMClient:
    def __init__(self, model: str, api_key: str | None = None, **default_kwargs: Any) -> None:
        self._model = model
        self._api_key = api_key
        self._default_kwargs = default_kwargs

        import litellm

        # Resolved once at construction via litellm's own provider
        # resolver (not a hand-maintained prefix string) -- the returned
        # dynamic api key is discarded here, never logged or returned:
        # for the `chatgpt` provider it is a live OAuth access token read
        # from the local device-flow cache.
        _, provider, _, _ = litellm.get_llm_provider(model)
        self._is_chatgpt_subscription = provider == _CHATGPT_PROVIDER

    async def complete(
        self,
        *,
        messages: list[dict[str, Any]],
        system: str | None = None,
        **kwargs: Any,
    ) -> str:
        if self._is_chatgpt_subscription:
            return await self._chatgpt_stream_text(messages, system=system, kwargs=kwargs)

        import litellm

        full_messages = ([{"role": "system", "content": system}] if system else []) + messages
        response = await litellm.acompletion(
            model=self._model,
            messages=full_messages,
            api_key=self._api_key,
            **{**self._default_kwargs, **kwargs},
        )
        return response.choices[0].message.content or ""

    async def complete_structured(
        self,
        *,
        messages: list[dict[str, Any]],
        response_schema: dict[str, Any],
        system: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        if self._is_chatgpt_subscription:
            # Verified directly: unlike a plain OpenAI API key's
            # `response_format`, this backend's `text.format=json_object`
            # is not strictly enforced on its own -- without an explicit
            # textual instruction the model answers in plain prose (e.g.
            # the bare step_id text, not `{"step_id": ...}`) and every
            # `json.loads()` call fails. `response_schema` was already an
            # argument here; it was just never used until now.
            json_instruction = (
                "Respond with a single JSON object only -- no prose, no "
                f"markdown fences -- matching this JSON Schema: "
                f"{json.dumps(response_schema)}"
            )
            augmented_system = (
                f"{system}\n\n{json_instruction}" if system else json_instruction
            )
            text = await self._chatgpt_stream_text(
                messages,
                system=augmented_system,
                kwargs=kwargs,
                text_format={"type": "json_object"},
            )
            return json.loads(text or "{}")

        import litellm

        full_messages = ([{"role": "system", "content": system}] if system else []) + messages
        response = await litellm.acompletion(
            model=self._model,
            messages=full_messages,
            api_key=self._api_key,
            response_format={"type": "json_object"},
            **{**self._default_kwargs, **kwargs},
        )
        content = response.choices[0].message.content or "{}"
        return json.loads(content)

    async def _chatgpt_stream_text(
        self,
        messages: list[dict[str, Any]],
        *,
        system: str | None,
        kwargs: dict[str, Any],
        text_format: dict[str, Any] | None = None,
    ) -> str:
        """``chatgpt/`` subscription models only — see the module docstring
        for why this bypasses ``acompletion()``. ``system`` becomes the
        Responses API's top-level ``instructions``, not an in-``input``
        message (the backend's own hardcoded Codex persona takes the
        ``input`` messages; ``instructions`` is the supported override
        point).
        """
        import litellm

        call_kwargs: dict[str, Any] = {**self._default_kwargs, **kwargs}
        call_kwargs.pop("messages", None)
        if text_format is not None:
            call_kwargs["text"] = {"format": text_format}

        response = await litellm.aresponses(
            model=self._model,
            input=messages,
            instructions=system,
            api_key=self._api_key,
            **call_kwargs,
        )

        text_parts: list[str] = []
        async for chunk in response:
            delta = getattr(chunk, "delta", None)
            if delta:
                text_parts.append(delta)
        return "".join(text_parts)
