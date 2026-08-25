"""GPT 백엔드. 이 파일 밖에서 `openai` SDK를 import 하지 않는다 (§2, §19)."""
from __future__ import annotations

from typing import TypeVar

import openai
from pydantic import BaseModel, ValidationError

from finredteam.llm._json import extract_json_array
from finredteam.llm.base import Usage

SchemaT = TypeVar("SchemaT", bound=BaseModel)


class OpenAIClient:
    provider = "openai"

    def __init__(self, model: str, api_key: str, max_tokens: int = 4096) -> None:
        self.model = model
        self._client = openai.OpenAI(api_key=api_key)
        self._max_tokens = max_tokens
        self._usage = Usage()

    def complete_json(
        self,
        system: str,
        user: str,
        schema: type[SchemaT],
        temperature: float = 0.3,
    ) -> list[SchemaT]:
        # 배열을 감싸는 최상위 객체가 필요한 벤더 제약(response_format=json_schema)이 있어
        # {"items": [...]} 래퍼로 요청하고, 실패 시 자유 JSON 텍스트 방식으로 재시도한다.
        item_schema = schema.model_json_schema()
        wrapper_schema = {
            "type": "object",
            "properties": {"items": {"type": "array", "items": item_schema}},
            "required": ["items"],
        }

        for attempt in range(2):
            try:
                if attempt == 0:
                    response = self._client.chat.completions.create(
                        model=self.model,
                        max_tokens=self._max_tokens,
                        temperature=temperature,
                        messages=[
                            {"role": "system", "content": system},
                            {"role": "user", "content": user},
                        ],
                        response_format={
                            "type": "json_schema",
                            "json_schema": {
                                "name": "candidate_list",
                                "schema": wrapper_schema,
                            },
                        },
                    )
                else:
                    instructed_system = (
                        f"{system}\n\n"
                        "반드시 JSON 배열만 출력하라. 각 원소는 아래 JSON Schema를 만족해야 한다.\n"
                        f"{item_schema}\n설명, 코드펜스 없이 배열 하나만 출력하라."
                    )
                    response = self._client.chat.completions.create(
                        model=self.model,
                        max_tokens=self._max_tokens,
                        temperature=temperature,
                        messages=[
                            {"role": "system", "content": instructed_system},
                            {"role": "user", "content": user},
                        ],
                    )
            except openai.BadRequestError:
                # response_format=json_schema 미지원 모델 — 자유 JSON 텍스트 방식으로 폴백
                instructed_system = (
                    f"{system}\n\n반드시 JSON 배열만 출력하라.\n{item_schema}"
                )
                response = self._client.chat.completions.create(
                    model=self.model,
                    max_tokens=self._max_tokens,
                    temperature=temperature,
                    messages=[
                        {"role": "system", "content": instructed_system},
                        {"role": "user", "content": user},
                    ],
                )

            text = response.choices[0].message.content or ""
            usage = response.usage
            self._usage = self._usage + Usage(
                tokens_in=usage.prompt_tokens if usage else 0,
                tokens_out=usage.completion_tokens if usage else 0,
                requests=1,
            )

            items = extract_json_array(text)
            if items is None:
                continue
            if len(items) == 1 and isinstance(items[0], dict) and "items" in items[0]:
                items = items[0]["items"]

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
