"""Claude 백엔드. 이 파일 밖에서 `anthropic` SDK를 import 하지 않는다 (§2, §19)."""
from __future__ import annotations

import logging
import time
from typing import TypeVar

import anthropic
from pydantic import BaseModel, ValidationError

from finredteam.config import LLM_MAX_RETRIES, LLM_REQUEST_TIMEOUT
from finredteam.llm._json import extract_json_array
from finredteam.llm.base import LLMError, LLMEvent, LLMEventKind, LLMTraceHook, Usage

logger = logging.getLogger(__name__)

SchemaT = TypeVar("SchemaT", bound=BaseModel)


class AnthropicClient:
    provider = "anthropic"

    def __init__(
        self,
        model: str,
        api_key: str | None = None,
        auth_token: str | None = None,
        max_tokens: int = 4096,
        trace_hook: LLMTraceHook | None = None,
    ) -> None:
        """`api_key` 는 `x-api-key` 헤더(전통적인 `sk-ant-...`), `auth_token` 은
        `Authorization: Bearer` 헤더(`claude setup-token` 으로 만든 OAuth 토큰)
        — 정확히 둘 중 하나만 있어야 한다. 어느 쪽이든 SDK 에 그대로 넘길 뿐,
        여기서 헤더를 직접 조립하지 않는다."""
        if not api_key and not auth_token:
            raise ValueError("api_key 또는 auth_token 중 하나는 있어야 함")
        self.model = model
        self._client = anthropic.Anthropic(
            api_key=api_key, auth_token=auth_token, timeout=LLM_REQUEST_TIMEOUT, max_retries=LLM_MAX_RETRIES
        )
        self._max_tokens = max_tokens
        self._usage = Usage()
        self._trace_hook = trace_hook

    def set_trace_hook(self, trace_hook: LLMTraceHook | None) -> None:
        """생성 이후에도 훅을 달 수 있게 — Engagement Setup 대화에 쓴 클라이언트를
        ReAct 실행에도 그대로 재사용하되, `RunStore` 가 생긴 뒤에야 훅을 달 수
        있을 때 쓴다(`__main__.py::main`)."""
        self._trace_hook = trace_hook

    def _emit(self, **fields: object) -> None:
        if self._trace_hook is None:
            return
        self._trace_hook(LLMEvent(provider=self.provider, model=self.model, **fields))

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
        self._emit(kind=LLMEventKind.CALL_STARTED, system_prompt=instructed_system, user_prompt=user)
        started_at = time.monotonic()

        for attempt in range(1, 3):
            try:
                # ★ `temperature` 를 최상위 키워드로 안 보낸다 — 설치된 SDK 버전에 따라
                # `Messages.create()` 시그니처에 `temperature` 자체가 없을 수 있다(신규
                # 메이저 버전, `thinking` 등 새 파라미터로 개편되며 빠짐). `extra_body`
                # 로 보내면 SDK 가 뭘 타입으로 노출하든 요청 본문에 그대로 실린다 — 서버가
                # 그래도 이 필드를 거부하면(예: 모델이 강제 reasoning 모드라 온도 고정)
                # `anthropic.APIError` 로 내려오고 그건 바로 아래에서 `LLMError` 로 잡는다.
                response = self._client.messages.create(
                    model=self.model,
                    max_tokens=self._max_tokens,
                    system=instructed_system,
                    messages=[{"role": "user", "content": user}],
                    extra_body={"temperature": temperature},
                )
            except anthropic.APIError as exc:
                # ★ 벤더 API 자체의 실패(네트워크·레이트리밋·타임아웃·인증) —
                # 우리 코드의 버그가 아니다. LLMError 로 감싸서 호출부가
                # "이번 스텝만 실패 처리하고 계속 진행"할 수 있게 한다.
                latency_ms = (time.monotonic() - started_at) * 1000
                self._emit(kind=LLMEventKind.CALL_FINISHED, attempt=attempt, error=str(exc), latency_ms=latency_ms)
                raise LLMError(f"Anthropic API 호출 실패: {exc}") from exc

            text = "".join(block.text for block in response.content if block.type == "text")
            self._usage = self._usage + Usage(
                tokens_in=response.usage.input_tokens,
                tokens_out=response.usage.output_tokens,
                requests=1,
            )

            items = extract_json_array(text)
            parsed: list[SchemaT] = []
            parse_error: str | None = None
            if items is None:
                parse_error = "JSON 배열을 찾지 못함"
            else:
                try:
                    for item in items:
                        parsed.append(schema.model_validate(item))
                except ValidationError as exc:
                    parsed = []
                    parse_error = str(exc)

            self._emit(
                kind=LLMEventKind.ATTEMPT_RESPONSE,
                attempt=attempt,
                raw_response=text,
                parsed_ok=parse_error is None,
                parse_error=parse_error,
                tokens_in=response.usage.input_tokens,
                tokens_out=response.usage.output_tokens,
            )
            logger.debug("Anthropic 응답 (attempt=%d): %s", attempt, text[:2000])
            if parse_error is not None:
                logger.warning("Anthropic 응답 파싱/검증 실패 (attempt=%d): %s", attempt, parse_error)

            if parse_error is None:
                self._emit(
                    kind=LLMEventKind.CALL_FINISHED,
                    attempt=attempt,
                    parsed_ok=True,
                    latency_ms=(time.monotonic() - started_at) * 1000,
                )
                return parsed

        self._emit(
            kind=LLMEventKind.CALL_FINISHED,
            attempt=2,
            parsed_ok=False,
            latency_ms=(time.monotonic() - started_at) * 1000,
        )
        return []

    @property
    def usage(self) -> Usage:
        return self._usage
