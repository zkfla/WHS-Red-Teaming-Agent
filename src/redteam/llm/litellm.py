"""``LLMClient`` backed by LiteLLM.

``litellm`` is imported lazily inside the methods, not at module scope:
it currently requires Python 3.11+ (``typing.NotRequired``), while the
rest of this package stays importable on 3.10. Install the ``llm``
extra (``pip install -e ".[llm]"``) on 3.11+ to use this class for real.
"""

from __future__ import annotations

import json
from typing import Any


class LiteLLMClient:
    def __init__(self, model: str, api_key: str | None = None, **default_kwargs: Any) -> None:
        self._model = model
        self._api_key = api_key
        self._default_kwargs = default_kwargs

    async def complete(
        self,
        *,
        messages: list[dict[str, Any]],
        system: str | None = None,
        **kwargs: Any,
    ) -> str:
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
