"""LLM Gateway port. Concrete implementations in ``redteam.llm``.

Keeping LiteLLM (or any provider SDK) call sites behind this Protocol is
what lets the model/provider be swapped without touching Agent code.

Provider failures cross this boundary only as :class:`LLMProviderFailure`:
kind, HTTP status, short provider code, retryable flag, and resume delay.
Unknown values stay ``unknown``/``None``. Full bodies, headers, and secrets
never cross here. Central callers read these fields; they never branch on
SDK exception types.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class StructuredCompletion:
    """Request-local model value and measured total tokens, when the provider reports it."""

    #: Raw text is allowed so format errors retain their measured request cost.
    value: dict[str, Any] | str
    total_tokens: int | None = None


_PROVIDER_KINDS = frozenset(
    {
        "rate_limited",
        "auth",
        "invalid_request",
        "server",
        "network",
        "timeout",
        "unknown",
    }
)


def _clean_kind(value: Any) -> str:
    if isinstance(value, str) and value in _PROVIDER_KINDS:
        return value
    return "unknown"


def _clean_http_status(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and 100 <= value <= 599:
        return value
    return None


def _clean_provider_code(value: Any) -> str:
    if not isinstance(value, str):
        return "unknown"
    text = value.strip()[:100]
    if not text:
        return "unknown"
    if all(char.isalnum() or char in "_-." for char in text):
        return text
    return "unknown"


def _clean_retry_after(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        if number != number or number == float("inf") or number == float("-inf"):
            return None
        if number < 0:
            return None
        return min(number, 3600.0)
    return None


class LLMProviderFailure(Exception):
    """Minimal public contract for a failed provider request.

    Adapters raise this instead of leaking SDK exceptions. Central code
    reads the fields without importing any SDK type. ``message`` is a
    fixed safe phrase plus status/code only; bodies, headers, prompts,
    and secrets are never stored here.
    """

    def __init__(
        self,
        *,
        kind: str = "unknown",
        http_status: int | None = None,
        provider_code: str = "unknown",
        retryable: bool = False,
        retry_after: float | None = None,
    ) -> None:
        self.kind = _clean_kind(kind)
        self.http_status = _clean_http_status(http_status)
        self.provider_code = _clean_provider_code(provider_code)
        self.retryable = bool(retryable)
        self.retry_after = _clean_retry_after(retry_after)
        status = str(self.http_status) if self.http_status is not None else "unknown"
        super().__init__(
            f"provider request failed (kind={self.kind}, "
            f"status={status}, code={self.provider_code})"
        )

    @property
    def provider_status(self) -> int | None:
        """Alias matching the diagnostics field name."""
        return self.http_status

    def diagnostic_fields(self) -> dict[str, Any]:
        """Public payload for the diagnostics owner to copy verbatim."""
        return {
            "provider_status": self.http_status,
            "provider_code": self.provider_code,
            "retry_after": self.retry_after,
        }


@runtime_checkable
class MeteredLLMClient(Protocol):
    """Optional telemetry path; existing JSON-only providers remain compatible."""

    async def complete_structured_with_usage(
        self,
        *,
        messages: list[dict[str, Any]],
        response_schema: dict[str, Any],
        system: str | None = None,
        **kwargs: Any,
    ) -> StructuredCompletion: ...


class LLMClient(Protocol):
    async def complete(
        self,
        *,
        messages: list[dict[str, Any]],
        system: str | None = None,
        **kwargs: Any,
    ) -> str:
        """Free-text completion."""
        ...

    async def complete_structured(
        self,
        *,
        messages: list[dict[str, Any]],
        response_schema: dict[str, Any],
        system: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Completion constrained to a JSON schema."""
        ...


# Native tool-calling contract (ReAct loop). Existing structured APIs are
# kept for explicit review flows; new model/tool-loop code uses
# ``complete_native`` and the frozen records below.

# Safe, fixed protocol/argument codes only. Raw provider payloads, prompts,
# and secrets are never stored in these fields.
NATIVE_PROTOCOL_MISSING_CALL_ID = "missing_call_id"
NATIVE_PROTOCOL_MISSING_TOOL_NAME = "missing_tool_name"
NATIVE_PROTOCOL_EMPTY_RESPONSE = "empty_response"
NATIVE_ARG_INVALID_ARGUMENTS = "invalid_arguments"

# Public assistant text is bounded so checkpoints/logs keep a masked,
# limited message instead of a full private response.
NATIVE_MAX_TEXT_CHARS = 20000

_NATIVE_PROTOCOL_ERRORS = frozenset(
    {
        NATIVE_PROTOCOL_MISSING_CALL_ID,
        NATIVE_PROTOCOL_MISSING_TOOL_NAME,
        NATIVE_PROTOCOL_EMPTY_RESPONSE,
    }
)

_NATIVE_ARGUMENT_ERRORS = frozenset({NATIVE_ARG_INVALID_ARGUMENTS})


def _clean_native_text(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    if len(value) > NATIVE_MAX_TEXT_CHARS:
        return value[:NATIVE_MAX_TEXT_CHARS]
    return value


def _clean_native_total_tokens(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value >= 0:
        return value
    return None


def _clean_native_protocol_error(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str) and value in _NATIVE_PROTOCOL_ERRORS:
        return value
    raise ValueError("unknown native protocol error")


def _clean_native_argument_error(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str) and value in _NATIVE_ARGUMENT_ERRORS:
        return value
    return NATIVE_ARG_INVALID_ARGUMENTS

# Opaque provider-owned continuation blocks carried without interpretation.
NATIVE_MAX_CONTINUATION_BLOCKS = 16
NATIVE_MAX_CONTINUATION_BYTES = 1048576
# 1 MiB bounds checkpoint growth without blocking max-effort long signatures,
# which can exceed the public-text budget. This is a checkpoint-safety bound,
# not a model-catalog limit, and there is no feature toggle for it.


def _check_continuation_json_keys(value: Any) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("continuation keys must be strings")
            _check_continuation_json_keys(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _check_continuation_json_keys(item)


def _clean_native_continuation(value: Any) -> tuple[dict[str, Any], ...]:
    # Opaque blocks; no provider branching here.
    if value is None:
        return ()
    if isinstance(value, list):
        value = tuple(value)
    if not isinstance(value, tuple):
        raise ValueError("bad continuation")
    if len(value) > NATIVE_MAX_CONTINUATION_BLOCKS:
        raise ValueError("continuation exceeds block limit")
    cleaned: list[dict[str, Any]] = []
    total = 0
    for block in value:
        if not isinstance(block, dict):
            raise ValueError("continuation must be tuple of dicts")
        _check_continuation_json_keys(block)
        try:
            payload = json.dumps(block, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ValueError("continuation must be JSON-shaped") from exc
        total += len(payload.encode("utf-8"))
        cleaned.append(json.loads(payload))
    if total > NATIVE_MAX_CONTINUATION_BYTES:
        raise ValueError("continuation exceeds size limit")
    return tuple(cleaned)


@dataclass(frozen=True)
class NativeToolSpec:
    """Exposed tool schema for a native model request."""

    name: str
    description: str = ""
    input_schema: dict[str, Any] = field(default_factory=dict)  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name:
            raise ValueError("NativeToolSpec.name must be a non-empty string")
        if not isinstance(self.description, str):
            raise ValueError("NativeToolSpec.description must be a string")
        if not isinstance(self.input_schema, dict):
            raise ValueError("NativeToolSpec.input_schema must be a dict")


@dataclass(frozen=True)
class NativeToolCall:
    """One provider-requested tool invocation with a stable call ID."""

    call_id: str
    name: str
    arguments: dict[str, Any] | None
    argument_error: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.call_id, str) or not self.call_id:
            raise ValueError("NativeToolCall.call_id must be a non-empty string")
        if not isinstance(self.name, str) or not self.name:
            raise ValueError("NativeToolCall.name must be a non-empty string")
        cleaned_error = _clean_native_argument_error(self.argument_error)
        object.__setattr__(self, "argument_error", cleaned_error)
        if cleaned_error is not None:
            object.__setattr__(self, "arguments", None)
        elif not isinstance(self.arguments, dict):
            raise ValueError("NativeToolCall.arguments must be a dict when argument_error is None")


@dataclass(frozen=True)
class NativeCompletion:
    """Normalized native model response: tool calls plus public text."""
    # continuation carries opaque provider-owned blocks unchanged to the next
    # request. Empty by default so older runs stay compatible. repr=False keeps
    # ciphertext out of logs; block meaning is owned by the SDK adapter.

    tool_calls: tuple[NativeToolCall, ...] = ()
    text: str = ""
    total_tokens: int | None = None
    protocol_error: str | None = None
    continuation: tuple[dict[str, Any], ...] = field(default=(), repr=False)

    def __post_init__(self) -> None:
        calls = self.tool_calls
        if isinstance(calls, list):
            calls = tuple(calls)
            object.__setattr__(self, "tool_calls", calls)
        if not isinstance(calls, tuple) or any(
            not isinstance(item, NativeToolCall) for item in calls
        ):
            raise ValueError("NativeCompletion.tool_calls must be a tuple of NativeToolCall")
        object.__setattr__(self, "text", _clean_native_text(self.text))
        object.__setattr__(
            self, "total_tokens", _clean_native_total_tokens(self.total_tokens)
        )
        object.__setattr__(
            self, "protocol_error", _clean_native_protocol_error(self.protocol_error)
        )
        object.__setattr__(
            self, "continuation", _clean_native_continuation(self.continuation)
        )


@runtime_checkable
class NativeToolCallingLLMClient(Protocol):
    """Optional native tool-calling path; existing text/JSON clients stay compatible."""

    async def complete_native(
        self,
        messages: list[dict[str, Any]],
        tools: list[NativeToolSpec],
        system: str | None = None,
        **kwargs: Any,
    ) -> NativeCompletion: ...
