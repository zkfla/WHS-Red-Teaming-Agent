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
from collections.abc import AsyncIterator
from typing import Any, cast

_CHATGPT_PROVIDER = "chatgpt"


_NATIVE_JSON_SCHEMA_PROVIDERS = frozenset({"openai", "azure", "azure_ai", "chatgpt"})


def _supports_native_json_schema(model: str) -> bool:
    """Native-schema support from the model string only, no network.

    With a slash, the provider prefix before the first slash must be
    openai, azure, azure_ai, or chatgpt. Without a slash, a bare
    OpenAI-family id starting with gpt-, o1, o3, or o4 qualifies.
    Everything else uses JSON object mode plus validation. Conservative
    by design: unknown providers fall back to json_object.
    """
    lowered = model.lower()
    if "/" in lowered:
        provider = lowered.split("/", 1)[0]
        return provider in _NATIVE_JSON_SCHEMA_PROVIDERS
    return lowered.startswith(("gpt-", "o1", "o3", "o4"))


def _native_chat_completion_format(response_schema: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "structured_decision",
            "schema": response_schema,
            "strict": False,
        },
    }


def _native_responses_format(response_schema: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "json_schema",
        "name": "structured_decision",
        "schema": response_schema,
        "strict": False,
    }


def _parse_json_dict(text: str) -> dict[str, Any]:
    body = (text or "{}").strip()
    fence = chr(96) * 3
    if body.startswith(fence):
        lines = body.splitlines()
        if lines and lines[0].startswith(fence):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith(fence):
            lines = lines[:-1]
        body = chr(10).join(lines).strip() or "{}"
    parsed = json.loads(body)
    if not isinstance(parsed, dict):
        raise ValueError("expected JSON object")
    return parsed


# Responses streaming contract, typed events only: response.output_text.delta
# carries a str delta, and exactly one terminal event ends the stream
# (response.completed success; response.failed, response.incomplete, or a
# request-level error event never count as usable output). The non-streamed
# answer is judged by the top-level status field instead.
# Limits: litellm stays an optional lazy import, so shapes are read through
# one accessor and untyped or unknown events are ignored. A stream without
# response.completed never counts as success, even with text buffered.


def _chunk_field(chunk: Any, name: str) -> Any:
    if isinstance(chunk, dict):
        return chunk.get(name)
    return getattr(chunk, name, None)


def _event_error_detail(chunk: Any) -> str | None:
    event = _chunk_field(chunk, "response")
    error = _chunk_field(event, "error") if event is not None else None
    if error is None:
        error = _chunk_field(chunk, "error")
    if error is None:
        detail = _chunk_field(chunk, "incomplete_details")
        return str(detail) if detail is not None else None
    if isinstance(error, dict):
        return str(error.get("message") or error)
    message = getattr(error, "message", None)
    return str(message if message is not None else error)


def _responses_output_text(response: Any) -> str | None:
    output = _chunk_field(response, "output")
    if not isinstance(output, (list, tuple)):
        return None
    parts: list[str] = []
    for item in output:
        content = _chunk_field(item, "content")
        if not isinstance(content, (list, tuple)):
            continue
        for part in content:
            if _chunk_field(part, "type") == "output_text":
                text = _chunk_field(part, "text")
                if isinstance(text, str) and text:
                    parts.append(text)
    return "".join(parts) or None


class LiteLLMClient:
    def __init__(
        self,
        model: str,
        api_key: str | None = None,
        num_retries: int = 0,
        **default_kwargs: Any,
    ) -> None:
        self._model = model
        self._api_key = api_key
        self._num_retries = num_retries
        self._default_kwargs = default_kwargs

        import litellm

        # Resolved once at construction via litellm's own provider
        # resolver (not a hand-maintained prefix string) -- the returned
        # dynamic api key is discarded here, never logged or returned:
        # for the `chatgpt` provider it is a live OAuth access token read
        # from the local device-flow cache.
        _, provider, _, _ = litellm.get_llm_provider(model)
        self._is_chatgpt_subscription = provider == _CHATGPT_PROVIDER

    def _call_kwargs(self, kwargs: dict[str, Any]) -> dict[str, Any]:
        merged: dict[str, Any] = {**self._default_kwargs, **kwargs}
        merged.setdefault("num_retries", self._num_retries)
        return merged

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
            **self._call_kwargs(kwargs),
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
            augmented_system = f"{system}\n\n{json_instruction}" if system else json_instruction
            text = await self._chatgpt_stream_text(
                messages,
                system=augmented_system,
                kwargs=kwargs,
                text_format=_native_responses_format(response_schema),
            )
            return _parse_json_dict(text)

        import litellm

        full_messages = ([{"role": "system", "content": system}] if system else []) + messages
        call_kwargs = self._call_kwargs(kwargs)
        if "response_format" not in call_kwargs:
            if _supports_native_json_schema(self._model):
                call_kwargs["response_format"] = _native_chat_completion_format(response_schema)
            else:
                call_kwargs["response_format"] = {"type": "json_object"}
        response = await litellm.acompletion(
            model=self._model,
            messages=full_messages,
            api_key=self._api_key,
            **call_kwargs,
        )
        content = response.choices[0].message.content or "{}"
        return _parse_json_dict(content)

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

        call_kwargs: dict[str, Any] = self._call_kwargs(kwargs)
        call_kwargs.pop("messages", None)
        # Streaming is this route's contract: delta accumulation below needs the
        # iterator, while an explicit caller opt-out is still respected.
        call_kwargs.setdefault("stream", True)
        streaming = bool(call_kwargs.get("stream"))
        if text_format is not None:
            call_kwargs["text"] = {"format": text_format}

        response = await litellm.aresponses(
            model=self._model,
            input=messages,
            instructions=system,
            api_key=self._api_key,
            **call_kwargs,
        )

        # Only a completed stream counts as success: text without the terminal
        # event means truncation, and partial JSON must never pass as normal.
        if not streaming:
            status = _chunk_field(response, "status")
            if status != "completed":
                raise RuntimeError(f"chatgpt response not completed: {status or 'unknown status'}")
            text = _responses_output_text(response)
            if not text:
                raise RuntimeError("chatgpt response returned no text")
            return text

        text_parts: list[str] = []
        completed = False
        async for chunk in cast(AsyncIterator[Any], response):
            event_type = _chunk_field(chunk, "type")
            if event_type == "response.completed":
                completed = True
            elif event_type in ("response.failed", "error"):
                detail = _event_error_detail(chunk)
                raise RuntimeError(f"chatgpt stream failed: {detail or 'unknown error'}")
            elif event_type == "response.incomplete":
                detail = _event_error_detail(chunk)
                raise RuntimeError(f"chatgpt stream incomplete: {detail or 'truncated output'}")
            elif event_type == "response.output_text.delta":
                delta = _chunk_field(chunk, "delta")
                if isinstance(delta, str) and delta:
                    text_parts.append(delta)
        text = "".join(text_parts)
        if not completed:
            raise RuntimeError("chatgpt stream ended without completion")
        if not text:
            raise RuntimeError("chatgpt stream returned no text")
        return text
