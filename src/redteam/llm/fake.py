"""Deterministic ``LLMClient`` for tests and the stub-node vertical slice.

No network calls, no API key required. Real agent/planner logic is not
implemented at this stage (instruction.md section 14) — this stands in
for it wherever a Protocol needs an ``LLMClient``.
"""

from __future__ import annotations

from typing import Any


class FakeLLMClient:
    def __init__(
        self, canned_text: str = "ack", canned_structured: dict[str, Any] | None = None
    ) -> None:
        self._canned_text = canned_text
        self._canned_structured = canned_structured or {}

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
