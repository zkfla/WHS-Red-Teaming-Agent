"""Deterministic ``LLMClient`` for tests and the stub-node vertical slice.

No network calls, no API key required. Real agent/planner logic is not
implemented at this stage (instruction.md section 14) \u2014 this stands in
for it wherever a Protocol needs an ``LLMClient``.
"""

from __future__ import annotations

import asyncio
import json
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
        native_responses: list[NativeCompletion] | None = None,
        structured_responses: list[dict[str, Any]] | None = None,
        native_responses_by_task: dict[str, list[NativeCompletion]] | None = None,
    ) -> None:
        self._canned_text = canned_text
        self._canned_structured = canned_structured or {}
        self._canned_native_text = canned_native_text
        self._canned_native_calls = tuple(canned_native_calls or ())
        self._canned_native_protocol_error = canned_native_protocol_error
        self._canned_native_tokens = canned_native_tokens
        self.last_native_request: dict[str, Any] | None = None
        self.native_responses = list(native_responses) if native_responses is not None else None
        self.structured_responses = (
            list(structured_responses) if structured_responses is not None else None
        )
        self.native_responses_by_task = native_responses_by_task or {}
        self.requests: list[dict[str, Any]] = []
        self.requests_changed = asyncio.Condition()
        self.request_release = asyncio.Event()
        self.request_release.set()
        self.active_requests = 0
        self.peak_requests = 0

    async def _wait_response(self, kind, messages):
        async with self.requests_changed:
            self.requests.append({"kind": kind, "messages": list(messages)})
            self.active_requests += 1
            self.peak_requests = max(self.peak_requests, self.active_requests)
            self.requests_changed.notify_all()
        try:
            await self.request_release.wait()
        finally:
            self.active_requests -= 1

    async def wait_for_requests(self, count):
        async with self.requests_changed:
            await self.requests_changed.wait_for(lambda: len(self.requests) >= count)

    async def complete(
        self,
        *,
        messages: list[dict[str, Any]],
        system: str | None = None,
        **kwargs: Any,
    ) -> str:
        await self._wait_response("complete", messages)
        return self._canned_text

    async def complete_structured(
        self,
        *,
        messages: list[dict[str, Any]],
        response_schema: dict[str, Any],
        system: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        await self._wait_response("complete_structured", messages)
        if self.structured_responses is not None:
            if not self.structured_responses:
                raise ValueError("fake_structured_response_exhausted")
            return self.structured_responses.pop(0)
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
        await self._wait_response("complete_native", messages)
        # Route only synthetic scripts using the actual structured Task ID.
        # This double has no scheduling, ownership or execution authority.
        for message in reversed(messages):
            if message.get("role") != "user":
                continue
            try:
                context = json.loads(message.get("content") or "{}")
            except (TypeError, ValueError):
                continue
            task_id = (context.get("active_task") or {}).get("task_id")
            if task_id in self.native_responses_by_task:
                script = self.native_responses_by_task[task_id]
                return (
                    script.pop(0)
                    if script
                    else NativeCompletion(
                        text="Development script exhausted.",
                    )
                )
            break
        if self.native_responses is not None:
            if not self.native_responses:
                return NativeCompletion(text="Development script exhausted.")
            return self.native_responses.pop(0)
        return NativeCompletion(
            tool_calls=tuple(self._canned_native_calls),
            text=self._canned_native_text,
            total_tokens=self._canned_native_tokens,
            protocol_error=self._canned_native_protocol_error,
        )
