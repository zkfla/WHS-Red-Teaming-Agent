"""LLMClient 프로토콜 — planner.py, recon/mapper.py 등 호출부는 이것만 안다.

벤더 SDK(anthropic, openai)는 llm/ 밖에서 직접 import 하지 않는다 (§2, §19).
"""
from __future__ import annotations

from typing import Protocol, TypeVar, runtime_checkable

from pydantic import BaseModel

SchemaT = TypeVar("SchemaT", bound=BaseModel)


class Usage(BaseModel):
    tokens_in: int = 0
    tokens_out: int = 0
    cost_estimate_usd: float = 0.0
    requests: int = 0

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(
            tokens_in=self.tokens_in + other.tokens_in,
            tokens_out=self.tokens_out + other.tokens_out,
            cost_estimate_usd=self.cost_estimate_usd + other.cost_estimate_usd,
            requests=self.requests + other.requests,
        )


@runtime_checkable
class LLMClient(Protocol):
    provider: str
    model: str

    def complete_json(
        self,
        system: str,
        user: str,
        schema: type[SchemaT],
        temperature: float = 0.3,
    ) -> list[SchemaT]:
        """구조화 출력 전용. 호출부가 쓰는 메서드는 이것 하나뿐이다.

        내부에서 파싱 실패 시 1회 재시도, 그래도 실패하면 빈 리스트를 반환한다
        (예외를 던지지 않는다 — 호출부가 매 후보마다 방어 코드를 반복하지 않도록).
        """
        ...

    @property
    def usage(self) -> Usage: ...
