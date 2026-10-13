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
from collections.abc import AsyncIterator, Mapping
from typing import Any, cast

from redteam.core.ports.llm import (
    NATIVE_ARG_INVALID_ARGUMENTS,
    NATIVE_PROTOCOL_EMPTY_RESPONSE,
    NATIVE_PROTOCOL_MISSING_CALL_ID,
    NATIVE_PROTOCOL_MISSING_TOOL_NAME,
    LLMProviderFailure,
    NativeCompletion,
    NativeToolCall,
    NativeToolSpec,
    StructuredCompletion,
)

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


def _extract_total_tokens(response: Any) -> int | None:
    """Read the provider-reported total tokens for one request, if present.

    Returns None when the SDK response carries no usable total: the
    request cost is unknown, never zero. Only non-negative integers
    count; anything else (missing, bool, negative, non-numeric) is
    unknown.
    """
    usage = _chunk_field(response, "usage")
    if usage is None:
        return None
    total = _chunk_field(usage, "total_tokens")
    if type(total) is int and total >= 0:
        return total
    # LiteLLM/provider SDK combinations do not always populate total_tokens.
    # Accept the two public usage shapes only when both halves are present;
    # a partial count remains unknown rather than silently under-counting.
    for input_key, output_key in (
        ("input_tokens", "output_tokens"),
        ("prompt_tokens", "completion_tokens"),
    ):
        input_tokens = _chunk_field(usage, input_key)
        output_tokens = _chunk_field(usage, output_key)
        if (
            type(input_tokens) is int
            and input_tokens >= 0
            and type(output_tokens) is int
            and output_tokens >= 0
        ):
            return input_tokens + output_tokens
    return None


# Native tool-calling normalization. Both transports (plain ``acompletion``
# tool_calls and subscription ``aresponses`` function-call items/streams)
# collapse to ``NativeCompletion``. Only safe, fixed codes cross here;
# raw arguments/text are never copied into error fields and no private
# reasoning or full SDK payloads are persisted.


def _parse_native_arguments(raw: Any) -> tuple[dict[str, Any] | None, str | None]:
    """Parse one tool-call argument payload without retaining raw text."""
    if raw is None:
        return {}, None
    if isinstance(raw, dict):
        return dict(raw), None
    if isinstance(raw, str):
        stripped = raw.strip()
        if not stripped:
            return {}, None
        try:
            parsed = json.loads(stripped)
        except (TypeError, ValueError):
            return None, NATIVE_ARG_INVALID_ARGUMENTS
        if not isinstance(parsed, dict):
            return None, NATIVE_ARG_INVALID_ARGUMENTS
        return parsed, None
    return None, NATIVE_ARG_INVALID_ARGUMENTS


def _native_call_from_parts(
    call_id: Any, name: Any, arguments_raw: Any
) -> tuple[NativeToolCall | None, str | None]:
    """Build one ``NativeToolCall``; missing IDs become protocol flags."""
    if not isinstance(call_id, str) or not call_id:
        return None, NATIVE_PROTOCOL_MISSING_CALL_ID
    if not isinstance(name, str) or not name:
        return None, NATIVE_PROTOCOL_MISSING_TOOL_NAME
    args, arg_error = _parse_native_arguments(arguments_raw)
    if arg_error is not None:
        return NativeToolCall(
            call_id=call_id, name=name, arguments=None, argument_error=arg_error
        ), None
    assert args is not None
    return NativeToolCall(
        call_id=call_id, name=name, arguments=args, argument_error=None
    ), None


def _coerce_schema_object(schema: Any) -> dict[str, Any]:
    if isinstance(schema, dict) and schema:
        return schema
    return {"type": "object"}


def _chat_tools_param(tools: list[NativeToolSpec]) -> list[dict[str, Any]] | None:
    if not tools:
        return None
    params: list[dict[str, Any]] = []
    for spec in tools:
        params.append(
            {
                "type": "function",
                "function": {
                    "name": spec.name,
                    "description": spec.description,
                    "parameters": _coerce_schema_object(spec.input_schema),
                },
            }
        )
    return params


def _responses_tools_param(tools: list[NativeToolSpec]) -> list[dict[str, Any]] | None:
    if not tools:
        return None
    params: list[dict[str, Any]] = []
    for spec in tools:
        params.append(
            {
                "type": "function",
                "name": spec.name,
                "description": spec.description,
                "parameters": _coerce_schema_object(spec.input_schema),
            }
        )
    return params


def _stringify_tool_arguments(arguments: Any) -> str:
    if isinstance(arguments, str):
        return arguments
    if isinstance(arguments, dict):
        try:
            return json.dumps(arguments)
        except (TypeError, ValueError):
            return "{}"
    return "{}"


def _stringify_tool_output(content: Any) -> str:
    if isinstance(content, str):
        return content
    if content is None:
        return ""
    try:
        return json.dumps(content)
    except (TypeError, ValueError):
        return str(content)


def _responses_input_from_chat(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert canonical chat messages to Responses input items."""
    converted: list[dict[str, Any]] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        item_type = message.get("type")
        if item_type in ("function_call", "function_call_output", "message"):
            converted.append(dict(message))
            continue
        role = message.get("role")
        if role in ("user", "system", "developer"):
            converted.append({"role": role, "content": message.get("content", "")})
            continue
        if role == "assistant":
            content = message.get("content")
            if isinstance(content, str) and content:
                converted.append({"role": "assistant", "content": content})
            elif isinstance(content, list) and content:
                converted.append({"role": "assistant", "content": content})
            raw_calls = message.get("tool_calls")
            if isinstance(raw_calls, (list, tuple)):
                for entry in raw_calls:
                    if not isinstance(entry, dict):
                        continue
                    call_id = entry.get("id")
                    if not isinstance(call_id, str) or not call_id:
                        continue
                    function = entry.get("function")
                    if not isinstance(function, dict):
                        continue
                    name = function.get("name")
                    if not isinstance(name, str) or not name:
                        continue
                    converted.append(
                        {
                            "type": "function_call",
                            "call_id": call_id,
                            "name": name,
                            "arguments": _stringify_tool_arguments(
                                function.get("arguments")
                            ),
                        }
                    )
            continue
        if role == "tool":
            call_id = message.get("tool_call_id")
            if not isinstance(call_id, str) or not call_id:
                continue
            converted.append(
                {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": _stringify_tool_output(message.get("content")),
                }
            )
            continue
    return converted


def _build_native_completion(
    *,
    calls: list[NativeToolCall],
    text: Any,
    total_tokens: Any,
    missing_call_id: bool = False,
    missing_tool_name: bool = False,
) -> NativeCompletion:
    protocol_error: str | None = None
    if missing_call_id:
        protocol_error = NATIVE_PROTOCOL_MISSING_CALL_ID
    elif missing_tool_name:
        protocol_error = NATIVE_PROTOCOL_MISSING_TOOL_NAME
    cleaned_text = text if isinstance(text, str) else ""
    if not calls and not cleaned_text.strip() and protocol_error is None:
        protocol_error = NATIVE_PROTOCOL_EMPTY_RESPONSE
    return NativeCompletion(
        tool_calls=tuple(calls),
        text=cleaned_text,
        total_tokens=total_tokens,
        protocol_error=protocol_error,
    )


def _normalize_chat_tool_calls(raw_calls: Any) -> tuple[list[NativeToolCall], bool, bool]:
    calls: list[NativeToolCall] = []
    missing_id = False
    missing_name = False
    if not isinstance(raw_calls, (list, tuple)):
        return calls, missing_id, missing_name
    for entry in raw_calls:
        function = _chunk_field(entry, "function")
        name = _chunk_field(function, "name") if function is not None else None
        arguments_raw = _chunk_field(function, "arguments") if function is not None else None
        call_id = _chunk_field(entry, "id")
        call, missing = _native_call_from_parts(call_id, name, arguments_raw)
        if missing == NATIVE_PROTOCOL_MISSING_CALL_ID:
            missing_id = True
            continue
        if missing == NATIVE_PROTOCOL_MISSING_TOOL_NAME:
            missing_name = True
            continue
        assert call is not None
        calls.append(call)
    return calls, missing_id, missing_name


def _normalize_responses_output_items(
    output: Any,
) -> tuple[list[NativeToolCall], bool, bool]:
    calls: list[NativeToolCall] = []
    missing_id = False
    missing_name = False
    if not isinstance(output, (list, tuple)):
        return calls, missing_id, missing_name
    for item in output:
        if _chunk_field(item, "type") != "function_call":
            continue
        call_id = _chunk_field(item, "call_id")
        name = _chunk_field(item, "name")
        arguments_raw = _chunk_field(item, "arguments")
        call, missing = _native_call_from_parts(call_id, name, arguments_raw)
        if missing == NATIVE_PROTOCOL_MISSING_CALL_ID:
            missing_id = True
            continue
        if missing == NATIVE_PROTOCOL_MISSING_TOOL_NAME:
            missing_name = True
            continue
        assert call is not None
        calls.append(call)
    return calls, missing_id, missing_name


# Provider-failure normalization (F1). LiteLLM 1.104.0 library source
# (litellm/exceptions.py) shapes this duck-typed read: every provider error
# carries ``status_code`` (401/403/404/400/422/408/429/500/502/503, ...),
# optional short ``code``/``type``, optional ``headers`` mapping with
# ``retry-after`` on 429/timeout, and an httpx ``response``. We read only
# those minimal fields. Full message/body/headers/prompts are never stored.
# Unknown status/code stay unknown. Mapping is generic HTTP semantics only:
# no model/provider-name branching. Central code reads LLMProviderFailure
# fields and never imports SDK types.


def _safe_status(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and 100 <= value <= 599:
        return value
    return None


def _safe_code(value: Any) -> str:
    if not isinstance(value, str):
        return "unknown"
    text = value.strip()[:100]
    if not text:
        return "unknown"
    if all(char.isalnum() or char in "_-." for char in text):
        return text
    return "unknown"


def _retry_after_from_headers(headers: Any) -> float | None:
    try:
        items = dict(headers) if isinstance(headers, Mapping) else None
    except Exception:
        return None
    if not items:
        return None
    lowered = {str(key).lower(): val for key, val in items.items()}
    for key in ("retry-after", "retry_after", "ratelimit-reset", "retry-after-ms"):
        if key not in lowered:
            continue
        raw = lowered[key]
        try:
            number = float(str(raw).strip())
        except (TypeError, ValueError):
            continue
        if key == "retry-after-ms":
            number = number / 1000.0
        if number != number or number == float("inf") or number == float("-inf"):
            continue
        if number < 0:
            continue
        return min(number, 3600.0)
    return None


def _classify_status(status: int | None) -> tuple[str, bool]:
    if status is None:
        return ("unknown", True)
    if status == 429:
        return ("rate_limited", True)
    if status in (401, 403):
        return ("auth", False)
    if status in (400, 404, 409, 413, 422):
        return ("invalid_request", False)
    if status in (408, 504):
        return ("timeout", True)
    if status in (500, 502, 503):
        return ("server", True)
    if 500 <= status <= 599:
        return ("server", True)
    if 400 <= status <= 499:
        return ("invalid_request", False)
    return ("unknown", True)


def _normalize_provider_error(exc: BaseException) -> LLMProviderFailure:
    if isinstance(exc, LLMProviderFailure):
        return exc
    status = _safe_status(getattr(exc, "status_code", None))
    if status is None:
        response = getattr(exc, "response", None)
        response_status = _chunk_field(response, "status_code") if response is not None else None
        status = _safe_status(response_status)
    raw_code = getattr(exc, "code", None)
    if raw_code is None:
        raw_code = getattr(exc, "type", None)
    code = _safe_code(raw_code)
    retry_after = _retry_after_from_headers(getattr(exc, "headers", None))
    if retry_after is None:
        retry_after = _retry_after_from_headers(
            getattr(getattr(exc, "response", None), "headers", None)
        )
    if retry_after is None:
        for attr in ("retry_after", "retry-after"):
            candidate = getattr(exc, attr, None)
            if isinstance(candidate, (int, float)) and not isinstance(candidate, bool):
                number = float(candidate)
                if 0 <= number <= 3600.0 and number == number:
                    retry_after = number
                    break
    kind, retryable = _classify_status(status)
    return LLMProviderFailure(
        kind=kind,
        http_status=status,
        provider_code=code,
        retryable=retryable,
        retry_after=retry_after,
    )


def _stream_protocol_failure() -> LLMProviderFailure:
    return LLMProviderFailure(
        kind="unknown",
        http_status=None,
        provider_code="unknown",
        retryable=True,
        retry_after=None,
    )


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
        if self._is_chatgpt_subscription:
            # Reuse the SDK's current OpenAI text-model catalog instead of
            # maintaining another list of model names. These aliases describe
            # capabilities; account access is still decided by the provider.
            subscription_models = {}
            for name, info in litellm.model_cost.items():
                if info.get("litellm_provider") != "openai" or info.get("mode") not in {
                    "chat",
                    "responses",
                }:
                    continue
                capabilities = {
                    key: value
                    for key, value in info.items()
                    if key.startswith("supports_")
                    or key in {"max_tokens", "max_input_tokens", "max_output_tokens"}
                }
                subscription_models[f"{provider}/{name.removeprefix('openai/')}"] = {
                    **capabilities,
                    "litellm_provider": provider,
                    "mode": "responses",
                    "supports_native_streaming": True,
                }
            # This endpoint requires native streaming. LiteLLM treats models
            # missing from its catalog as non-streaming and removes `stream`
            # from the request, even when the caller explicitly enabled it.
            # Register the provider's contract for the selected model using
            # the SDK's public API; no model-name allowlist is needed.
            subscription_models[model] = {
                **subscription_models.get(model, {}),
                "litellm_provider": provider,
                "mode": "responses",
                "supports_native_streaming": True,
            }
            litellm.register_model(subscription_models)

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
        try:
            response = await litellm.acompletion(
                model=self._model,
                messages=full_messages,
                api_key=self._api_key,
                **self._call_kwargs(kwargs),
            )
        except LLMProviderFailure:
            raise
        except Exception as exc:
            raise _normalize_provider_error(exc) from exc
        return response.choices[0].message.content or ""

    async def complete_structured_with_usage(
        self,
        *,
        messages: list[dict[str, Any]],
        response_schema: dict[str, Any],
        system: str | None = None,
        **kwargs: Any,
    ) -> StructuredCompletion:
        """Structured call plus this request's provider-reported usage.

        Mirrors complete_structured exactly (same subscription/standard
        paths, same JSON parsing) and additionally returns total_tokens:
        None when the SDK response carries no usable total. No shared
        last_usage state is kept; usage travels with this request's
        result for the caller to record on its own budget.
        """
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
            # The subscription streaming route surfaces text only, so the
            # request cost is unknown rather than zero.
            return StructuredCompletion(value=text, total_tokens=None)

        import litellm

        full_messages = ([{"role": "system", "content": system}] if system else []) + messages
        call_kwargs = self._call_kwargs(kwargs)
        if "response_format" not in call_kwargs:
            if _supports_native_json_schema(self._model):
                call_kwargs["response_format"] = _native_chat_completion_format(response_schema)
            else:
                call_kwargs["response_format"] = {"type": "json_object"}
        try:
            response = await litellm.acompletion(
                model=self._model,
                messages=full_messages,
                api_key=self._api_key,
                **call_kwargs,
            )
        except LLMProviderFailure:
            raise
        except Exception as exc:
            raise _normalize_provider_error(exc) from exc
        content = response.choices[0].message.content or "{}"
        return StructuredCompletion(
            value=content,
            total_tokens=_extract_total_tokens(response),
        )

    async def complete_structured(
        self,
        *,
        messages: list[dict[str, Any]],
        response_schema: dict[str, Any],
        system: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Completion constrained to a JSON schema (usage discarded).

        Kept for existing JSON-only callers; delegates to
        complete_structured_with_usage so both paths stay identical.
        """
        completion = await self.complete_structured_with_usage(
            messages=messages,
            response_schema=response_schema,
            system=system,
            **kwargs,
        )
        return (
            _parse_json_dict(completion.value)
            if isinstance(completion.value, str)
            else completion.value
        )

    async def complete_native(
        self,
        messages: list[dict[str, Any]],
        tools: list[NativeToolSpec],
        system: str | None = None,
        **kwargs: Any,
    ) -> NativeCompletion:
        """Native tool-calling completion; text without calls is terminal input."""
        if not isinstance(messages, list) or not isinstance(tools, list):
            raise ValueError("complete_native requires messages and tools lists")
        for spec in tools:
            if not isinstance(spec, NativeToolSpec):
                raise ValueError("complete_native tools must be NativeToolSpec")
        for message in messages:
            if not isinstance(message, dict):
                raise ValueError("complete_native messages must be dicts")
        if self._is_chatgpt_subscription:
            return await self._responses_complete_native(
                messages, tools, system=system, kwargs=kwargs
            )
        return await self._chat_complete_native(
            messages, tools, system=system, kwargs=kwargs
        )

    async def _chat_complete_native(
        self,
        messages: list[dict[str, Any]],
        tools: list[NativeToolSpec],
        *,
        system: str | None,
        kwargs: dict[str, Any],
    ) -> NativeCompletion:
        import litellm

        full_messages = ([{"role": "system", "content": system}] if system else []) + messages
        call_kwargs = self._call_kwargs(kwargs)
        call_kwargs.pop("previous_response_id", None)
        call_kwargs.pop("tools", None)
        tools_param = _chat_tools_param(tools)
        if tools_param is not None:
            call_kwargs["tools"] = tools_param
        try:
            response = await litellm.acompletion(
                model=self._model,
                messages=full_messages,
                api_key=self._api_key,
                **call_kwargs,
            )
        except LLMProviderFailure:
            raise
        except Exception as exc:
            raise _normalize_provider_error(exc) from exc
        try:
            message = response.choices[0].message
        except (AttributeError, IndexError, TypeError) as exc:
            raise _normalize_provider_error(exc) from exc
        content = _chunk_field(message, "content") or ""
        raw_calls = _chunk_field(message, "tool_calls")
        calls, missing_id, missing_name = _normalize_chat_tool_calls(raw_calls)
        return _build_native_completion(
            calls=calls,
            text=content,
            total_tokens=_extract_total_tokens(response),
            missing_call_id=missing_id,
            missing_tool_name=missing_name,
        )

    async def _responses_complete_native(
        self,
        messages: list[dict[str, Any]],
        tools: list[NativeToolSpec],
        *,
        system: str | None,
        kwargs: dict[str, Any],
    ) -> NativeCompletion:
        import litellm

        converted = _responses_input_from_chat(messages)
        call_kwargs: dict[str, Any] = self._call_kwargs(kwargs)
        call_kwargs.pop("messages", None)
        call_kwargs.pop("previous_response_id", None)
        call_kwargs.pop("tools", None)
        call_kwargs.pop("response_format", None)
        call_kwargs.pop("text", None)
        call_kwargs.pop("text_format", None)
        call_kwargs.setdefault("stream", True)
        streaming = bool(call_kwargs.get("stream"))
        tools_param = _responses_tools_param(tools)
        if tools_param is not None:
            call_kwargs["tools"] = tools_param
        try:
            response = await litellm.aresponses(
                model=self._model,
                input=converted,
                instructions=system,
                api_key=self._api_key,
                **call_kwargs,
            )
        except LLMProviderFailure:
            raise
        except Exception as exc:
            raise _normalize_provider_error(exc) from exc
        try:
            if not streaming:
                status = _chunk_field(response, "status")
                if status != "completed":
                    raise _stream_protocol_failure()
                text = _responses_output_text(response) or ""
                output = _chunk_field(response, "output")
                calls, missing_id, missing_name = _normalize_responses_output_items(
                    output
                )
                return _build_native_completion(
                    calls=calls,
                    text=text,
                    total_tokens=None,
                    missing_call_id=missing_id,
                    missing_tool_name=missing_name,
                )
            return await self._accumulate_responses_stream_native(response)
        except LLMProviderFailure:
            raise
        except Exception as exc:
            raise _normalize_provider_error(exc) from exc

    async def _accumulate_responses_stream_native(self, response: Any) -> NativeCompletion:
        """Accumulate text plus function-call items from a Responses stream."""
        text_parts: list[str] = []
        pending: dict[Any, dict[str, Any]] = {}
        completed = False

        def _pending_key(output_index: Any, item_id: Any) -> Any:
            if isinstance(output_index, int) and not isinstance(output_index, bool):
                return ("index", output_index)
            if isinstance(item_id, str) and item_id:
                return ("item", item_id)
            return None

        async for chunk in cast(AsyncIterator[Any], response):
            event_type = _chunk_field(chunk, "type")
            if event_type == "response.completed":
                completed = True
            elif event_type in ("response.failed", "error"):
                raise _stream_protocol_failure()
            elif event_type == "response.incomplete":
                raise _stream_protocol_failure()
            elif event_type == "response.output_text.delta":
                delta = _chunk_field(chunk, "delta")
                if isinstance(delta, str) and delta:
                    text_parts.append(delta)
            elif event_type == "response.output_item.added":
                item = _chunk_field(chunk, "item")
                if _chunk_field(item, "type") != "function_call":
                    continue
                key = _pending_key(
                    _chunk_field(chunk, "output_index"), _chunk_field(chunk, "item_id")
                )
                if key is None:
                    continue
                entry = pending.setdefault(key, {"args_parts": [], "final_args": None})
                call_id = _chunk_field(item, "call_id")
                if isinstance(call_id, str) and call_id:
                    entry["call_id"] = call_id
                name = _chunk_field(item, "name")
                if isinstance(name, str) and name:
                    entry["name"] = name
                arguments = _chunk_field(item, "arguments")
                if isinstance(arguments, str) and arguments:
                    entry["final_args"] = arguments
            elif event_type == "response.function_call_arguments.delta":
                key = _pending_key(
                    _chunk_field(chunk, "output_index"), _chunk_field(chunk, "item_id")
                )
                if key is None:
                    continue
                entry = pending.setdefault(key, {"args_parts": [], "final_args": None})
                delta = _chunk_field(chunk, "delta")
                if isinstance(delta, str) and delta:
                    entry["args_parts"].append(delta)
            elif event_type == "response.function_call_arguments.done":
                key = _pending_key(
                    _chunk_field(chunk, "output_index"), _chunk_field(chunk, "item_id")
                )
                if key is None:
                    continue
                entry = pending.setdefault(key, {"args_parts": [], "final_args": None})
                arguments = _chunk_field(chunk, "arguments")
                if isinstance(arguments, str):
                    entry["final_args"] = arguments
            elif event_type == "response.output_item.done":
                item = _chunk_field(chunk, "item")
                if _chunk_field(item, "type") != "function_call":
                    continue
                key = _pending_key(
                    _chunk_field(chunk, "output_index"), _chunk_field(chunk, "item_id")
                )
                if key is None:
                    continue
                entry = pending.setdefault(key, {"args_parts": [], "final_args": None})
                call_id = _chunk_field(item, "call_id")
                if isinstance(call_id, str) and call_id:
                    entry["call_id"] = call_id
                name = _chunk_field(item, "name")
                if isinstance(name, str) and name:
                    entry["name"] = name
                arguments = _chunk_field(item, "arguments")
                if isinstance(arguments, str) and arguments:
                    entry["final_args"] = arguments
        if not completed:
            raise _stream_protocol_failure()
        calls: list[NativeToolCall] = []
        missing_id = False
        missing_name = False
        for entry in pending.values():
            final_args = entry.get("final_args")
            if final_args is None:
                parts = entry.get("args_parts", [])
                joined = "".join(part for part in parts if isinstance(part, str))
                final_args = joined if joined else "{}"
            call, missing = _native_call_from_parts(
                entry.get("call_id"), entry.get("name"), final_args
            )
            if missing == NATIVE_PROTOCOL_MISSING_CALL_ID:
                missing_id = True
                continue
            if missing == NATIVE_PROTOCOL_MISSING_TOOL_NAME:
                missing_name = True
                continue
            assert call is not None
            calls.append(call)
        return _build_native_completion(
            calls=calls,
            text="".join(text_parts),
            total_tokens=None,
            missing_call_id=missing_id,
            missing_tool_name=missing_name,
        )

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

        try:
            response = await litellm.aresponses(
                model=self._model,
                input=messages,
                instructions=system,
                api_key=self._api_key,
                **call_kwargs,
            )
        except LLMProviderFailure:
            raise
        except Exception as exc:
            raise _normalize_provider_error(exc) from exc

        # Only a completed stream counts as success: text without the terminal
        # event means truncation, and partial JSON must never pass as normal.
        # Protocol truncations carry no HTTP status; they normalize to unknown
        # retryable without provider free-text detail.
        try:
            if not streaming:
                status = _chunk_field(response, "status")
                if status != "completed":
                    raise _stream_protocol_failure()
                text = _responses_output_text(response)
                if not text:
                    raise _stream_protocol_failure()
                return text

            text_parts: list[str] = []
            completed = False
            async for chunk in cast(AsyncIterator[Any], response):
                event_type = _chunk_field(chunk, "type")
                if event_type == "response.completed":
                    completed = True
                elif event_type in ("response.failed", "error"):
                    raise _stream_protocol_failure()
                elif event_type == "response.incomplete":
                    raise _stream_protocol_failure()
                elif event_type == "response.output_text.delta":
                    delta = _chunk_field(chunk, "delta")
                    if isinstance(delta, str) and delta:
                        text_parts.append(delta)
            text = "".join(text_parts)
            if not completed:
                raise _stream_protocol_failure()
            if not text:
                raise _stream_protocol_failure()
            return text
        except LLMProviderFailure:
            raise
        except Exception as exc:
            raise _normalize_provider_error(exc) from exc
