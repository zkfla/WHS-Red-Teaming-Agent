"""LLMClient 프로토콜 — planner.py, recon/mapper.py 등 호출부는 이것만 안다.

벤더 SDK(anthropic, openai)는 llm/ 밖에서 직접 import 하지 않는다 (§2, §19).
이 파일도 예외라 없다 — `LLMError` 는 우리가 정의한 타입이지, 벤더 예외를
그대로 노출하는 게 아니다(호출부가 `anthropic.RateLimitError` 를 알 필요는
없다).
"""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Callable, Protocol, TypeVar, runtime_checkable

from pydantic import BaseModel, Field

SchemaT = TypeVar("SchemaT", bound=BaseModel)


class LLMError(Exception):
    """API 자체의 실패(네트워크, 레이트리밋, 타임아웃, 인증 등) — 우리 코드의
    버그와 구분하기 위한 전용 타입이다.

    `complete_json` 내부에서 파싱/검증에 실패하는 것과는 다르다 — 그건 "LLM이
    이상한 걸 냈다"는 정상적인(예상 가능한) 실패라 예외 없이 빈 리스트로
    처리한다. `LLMError` 는 "API 호출 자체가 안 됐다"는, 우리 코드가 아니라
    바깥(네트워크/벤더) 이 원인인 실패다. 이 타입이 아닌 다른 예외가
    `complete_json` 에서 새어 나온다면 그건 우리 코드의 버그이므로 잡지 않고
    그대로 위로 전파되어야 한다(§ DEBUGGING.md).
    """


class LLMEventKind(str, Enum):
    CALL_STARTED = "call_started"
    ATTEMPT_RESPONSE = "attempt_response"
    CALL_FINISHED = "call_finished"


class LLMEvent(BaseModel):
    """LLM 호출의 시작/각 시도/종료를 알리는 이벤트. 사람이 디버깅할 때만
    쓰인다 — 판정 파이프라인은 이 값을 절대 읽지 않는다(§ DEBUGGING.md §2)."""

    kind: LLMEventKind
    provider: str
    model: str
    attempt: int = 0
    system_prompt: str | None = None
    user_prompt: str | None = None
    raw_response: str | None = None
    parsed_ok: bool | None = None
    parse_error: str | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None
    latency_ms: float | None = None
    error: str | None = None
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


LLMTraceHook = Callable[[LLMEvent], None]


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

        파싱/검증 실패 시 1회 재시도, 그래도 실패하면 빈 리스트를 반환한다
        (이 경우는 예외를 던지지 않는다 — "LLM이 이상한 걸 냈다"는 정상적으로
        예상 가능한 실패라, 호출부가 매번 방어 코드를 반복하지 않게 한다).

        반대로 API 호출 자체가 실패하면(네트워크, 레이트리밋, 타임아웃, 인증
        등) `LLMError` 를 던진다 — 이건 호출부가 명시적으로 처리해야 하는,
        LLM 출력 품질과는 무관한 별종의 실패다. `LLMError` 가 아닌 다른
        예외가 새어 나온다면 그건 우리 코드의 버그다(§ DEBUGGING.md).
        """
        ...

    @property
    def usage(self) -> Usage: ...
