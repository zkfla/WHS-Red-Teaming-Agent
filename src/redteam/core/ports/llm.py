"""LLM Gateway port. Concrete implementations in ``redteam.llm``.

Keeping LiteLLM (or any provider SDK) call sites behind this Protocol is
what lets the model/provider be swapped without touching Agent code.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class StructuredCompletion:
    """Request-local model value and measured total tokens, when the provider reports it."""

    value: dict[str, Any]
    total_tokens: int | None = None


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
