"""Deterministic ``LLMClient`` for tests and the stub-node vertical slice.

No network calls, no API key required. Real agent/planner logic is not
implemented at this stage (instruction.md section 14) \u2014 this stands in
for it wherever a Protocol needs an ``LLMClient``.
"""

from __future__ import annotations

from typing import Any

from redteam.core.ports.llm import NativeCompletion, NativeToolCall, NativeToolSpec


class FakeLLMClient:
    def __init__(
        self,
        canned_text: str = "ack",
        canned_structured: dict[str, Any] | None = None,
        canned_native_text: str = "ack",
        canned_native_calls: list[NativeToolCall] | tuple[NativeToolCall, ...] | None = None,
        canned_native_protocol_error: str | None = None,
        canned_native_tokens: int | None = None,
    ) -> None:
        self._canned_text = canned_text
        self._canned_structured = canned_structured or {}
        self._canned_native_text = canned_native_text
        self._canned_native_calls = tuple(canned_native_calls or ())
        self._canned_native_protocol_error = canned_native_protocol_error
        self._canned_native_tokens = canned_native_tokens
        self.last_native_request: dict[str, Any] | None = None

    async def complete(
        self,
        *,
        messages: list[dict[str, Any]],
        system: str | None = None,
        **kwargs: Any,
    ) -> str:
        return self._canned_text

    async def complete_structured(
        self,
        *,
        messages: list[dict[str, Any]],
        response_schema: dict[str, Any],
        system: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        return dict(self._canned_structured)

    async def complete_native(
        self,
        messages: list[dict[str, Any]],
        tools: list[NativeToolSpec],
        system: str | None = None,
        **kwargs: Any,
    ) -> NativeCompletion:
        """Scripted native response for adapter/loop tests (no network)."""
        self.last_native_request = {
            "messages": list(messages),
            "tools": list(tools),
            "system": system,
        }
        return NativeCompletion(
            tool_calls=tuple(self._canned_native_calls),
            text=self._canned_native_text,
            total_tokens=self._canned_native_tokens,
            protocol_error=self._canned_native_protocol_error,
        )
