"""LLM Gateway port. Concrete implementations in ``redteam.llm``.

Keeping LiteLLM (or any provider SDK) call sites behind this Protocol is
what lets the model/provider be swapped without touching Agent code.
"""

from __future__ import annotations

from typing import Any, Protocol


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
