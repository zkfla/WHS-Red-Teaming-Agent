"""GPT 백엔드. 이 파일 밖에서 `openai` SDK를 import 하지 않는다 (§2, §19)."""
from __future__ import annotations

import logging
import time
from typing import TypeVar

import openai
from pydantic import BaseModel, ValidationError

from finredteam.config import LLM_REQUEST_TIMEOUT
from finredteam.llm._json import extract_json_array
from finredteam.llm.base import LLMError, LLMEvent, LLMEventKind, LLMTraceHook, Usage

logger = logging.getLogger(__name__)

SchemaT = TypeVar("SchemaT", bound=BaseModel)


class OpenAIClient:
    provider = "openai"

    def __init__(
        self,
        model: str,
        api_key: str,
        max_tokens: int = 4096,
        trace_hook: LLMTraceHook | None = None,
    ) -> None:
        self.model = model
        self._client = openai.OpenAI(api_key=api_key, timeout=LLM_REQUEST_TIMEOUT)
        self._max_tokens = max_tokens
        self._usage = Usage()
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
        # 배열을 감싸는 최상위 객체가 필요한 벤더 제약(response_format=json_schema)이 있어
        # {"items": [...]} 래퍼로 요청하고, 모델이 그걸 지원하지 않으면(BadRequestError)
        # 자유 JSON 텍스트 방식으로 그 자리에서 폴백한다 — 이건 API 실패가 아니라 예상된
        # 분기라 LLMError 로 취급하지 않는다.
        item_schema = schema.model_json_schema()
        wrapper_schema = {
            "type": "object",
            "properties": {"items": {"type": "array", "items": item_schema}},
            "required": ["items"],
        }

        self._emit(kind=LLMEventKind.CALL_STARTED, system_prompt=system, user_prompt=user)
        started_at = time.monotonic()

        for attempt in range(1, 3):
            try:
                if attempt == 1:
                    try:
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
                                "json_schema": {"name": "candidate_list", "schema": wrapper_schema},
                            },
                        )
                    except openai.BadRequestError:
                        # response_format=json_schema 미지원 모델 — 자유 JSON 텍스트로 폴백
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
            except openai.APIError as exc:
                # ★ 벤더 API 자체의 실패(네트워크·레이트리밋·타임아웃·인증) —
                # 우리 코드의 버그가 아니다.
                latency_ms = (time.monotonic() - started_at) * 1000
                self._emit(kind=LLMEventKind.CALL_FINISHED, attempt=attempt, error=str(exc), latency_ms=latency_ms)
                raise LLMError(f"OpenAI API 호출 실패: {exc}") from exc

            text = response.choices[0].message.content or ""
            usage = response.usage
            tokens_in = usage.prompt_tokens if usage else 0
            tokens_out = usage.completion_tokens if usage else 0
            self._usage = self._usage + Usage(tokens_in=tokens_in, tokens_out=tokens_out, requests=1)

            items = extract_json_array(text)
            parsed: list[SchemaT] = []
            parse_error: str | None = None
            if items is None:
                parse_error = "JSON 배열을 찾지 못함"
            else:
                if len(items) == 1 and isinstance(items[0], dict) and "items" in items[0]:
                    items = items[0]["items"]
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
                tokens_in=tokens_in,
                tokens_out=tokens_out,
            )
            logger.debug("OpenAI 응답 (attempt=%d): %s", attempt, text[:2000])
            if parse_error is not None:
                logger.warning("OpenAI 응답 파싱/검증 실패 (attempt=%d): %s", attempt, parse_error)

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
