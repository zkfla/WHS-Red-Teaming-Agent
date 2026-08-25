"""Claude 백엔드. 이 파일 밖에서 `anthropic` SDK를 import 하지 않는다 (§2, §19)."""
from __future__ import annotations

from typing import TypeVar

import anthropic
from pydantic import BaseModel, ValidationError

from finredteam.llm._json import extract_json_array
from finredteam.llm.base import Usage

SchemaT = TypeVar("SchemaT", bound=BaseModel)


class AnthropicClient:
    provider = "anthropic"

    def __init__(self, model: str, api_key: str, max_tokens: int = 4096) -> None:
        self.model = model
        self._client = anthropic.Anthropic(api_key=api_key)
        self._max_tokens = max_tokens
        self._usage = Usage()

    def complete_json(
        self,
        system: str,
        user: str,
        schema: type[SchemaT],
        temperature: float = 0.3,
    ) -> list[SchemaT]:
        instructed_system = (
            f"{system}\n\n"
            "반드시 JSON 배열만 출력하라. 각 원소는 아래 JSON Schema를 만족해야 한다.\n"
            f"{schema.model_json_schema()}\n"
            "설명, 코드펜스, 그 외 텍스트를 절대 포함하지 마라. 배열 하나만 출력하라."
        )
        for _attempt in range(2):
            response = self._client.messages.create(
                model=self.model,
                max_tokens=self._max_tokens,
                temperature=temperature,
                system=instructed_system,
                messages=[{"role": "user", "content": user}],
            )
            text = "".join(
                block.text for block in response.content if block.type == "text"
            )
            self._usage = self._usage + Usage(
                tokens_in=response.usage.input_tokens,
                tokens_out=response.usage.output_tokens,
                requests=1,
            )

            items = extract_json_array(text)
            if items is None:
                continue
            parsed: list[SchemaT] = []
            try:
                for item in items:
                    parsed.append(schema.model_validate(item))
            except ValidationError:
                continue
            return parsed
        return []

    @property
    def usage(self) -> Usage:
        return self._usage
