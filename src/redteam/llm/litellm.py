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
import math
import time
from collections.abc import AsyncIterator, Callable, Mapping
from email.utils import parsedate_to_datetime
from typing import Any, cast

from redteam.core.ports.llm import LLMProviderFailure, ProviderRetryPolicy, StructuredCompletion
from redteam.llm.provider_backoff import ProviderBackoff

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
    if isinstance(total, bool) or not isinstance(total, int) or total < 0:
        return None
    return total


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
    for key in ("retry-after", "retry-after-ms"):
        if key not in lowered:
            continue
        raw = lowered[key]
        try:
            number = float(str(raw).strip())
        except (TypeError, ValueError):
            if key != "retry-after":
                continue
            try:
                number = max(0.0, parsedate_to_datetime(str(raw)).timestamp() - time.time())
            except (TypeError, ValueError, OverflowError):
                continue
        if key == "retry-after-ms":
            number = number / 1000.0
        if number != number or number == float("inf") or number == float("-inf"):
            continue
        if number < 0:
            continue
        return number
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
    # Read only structured identifiers, never parse or retain remote free text.
    body = getattr(exc, "body", None)
    error = body.get("error", body) if isinstance(body, Mapping) else {}
    body_code = _safe_code(error.get("code")) if isinstance(error, Mapping) else "unknown"
    body_type = _safe_code(error.get("type")) if isinstance(error, Mapping) else "unknown"
    if body_code != "unknown":
        code = body_code
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
                if number >= 0 and math.isfinite(number):
                    retry_after = number
                    break
    kind, retryable = _classify_status(status)
    quota_codes = {
        "insufficient_quota", "credit_balance_exhausted",
        "organization_spend_limit_exceeded", "project_spend_limit_exceeded",
        "organization_usage_limit_exceeded",
    }
    if status == 429 and ({code, body_type, _safe_code(getattr(exc, "type", None))}
                          & quota_codes):
        kind, retryable = "quota", False
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
        retry_policy: ProviderRetryPolicy | None = None,
        **default_kwargs: Any,
    ) -> None:
        self._model = model
        self._api_key = api_key
        self._num_retries = num_retries
        self._default_kwargs = default_kwargs
        self._provider_backoff = ProviderBackoff(retry_policy or ProviderRetryPolicy())

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

    @property
    def provider_wait_limit_seconds(self) -> float:
        return self._provider_backoff.policy.max_wait_seconds

    async def wait_for_provider(
        self, *, max_wait_seconds: float | None = None,
        before_call: Callable[..., Any] | None = None,
    ) -> float:
        return await self._provider_backoff.wait(
            max_wait_seconds=max_wait_seconds, before_call=before_call,
        )

    def _provider_failure(self, exc: BaseException) -> LLMProviderFailure:
        failure = _normalize_provider_error(exc)
        self._provider_backoff.failed(failure)
        return failure

    async def complete(
        self,
        *,
        messages: list[dict[str, Any]],
        system: str | None = None,
        **kwargs: Any,
    ) -> str:
        await self.wait_for_provider()
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
        except Exception as exc:
            failure = self._provider_failure(exc)
            if failure is exc:
                raise
            raise failure from exc
        self._provider_backoff.succeeded()
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
        await self.wait_for_provider()
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
        except Exception as exc:
            failure = self._provider_failure(exc)
            if failure is exc:
                raise
            raise failure from exc
        self._provider_backoff.succeeded()
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
        except Exception as exc:
            failure = self._provider_failure(exc)
            if failure is exc:
                raise
            raise failure from exc

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
                self._provider_backoff.succeeded()
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
            self._provider_backoff.succeeded()
            return text
        except Exception as exc:
            failure = self._provider_failure(exc)
            if failure is exc:
                raise
            raise failure from exc
