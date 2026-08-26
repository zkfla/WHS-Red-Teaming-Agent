"""ChatGPT(Codex) Subscription 백엔드 — `codex login` 이 만든 `~/.codex/auth.json`
을 인증정보로 써서, ChatGPT Plus/Pro 구독을 순수 LLM 추론 백엔드로만 쓴다.

★ Codex Agent Runtime(`codex exec` 등)은 쓰지 않는다 — 이 클라이언트는
`anthropic_.py`/`openai_.py` 와 정확히 같은 `LLMClient` 계약(`complete_json`)
하나만 구현한다. `agent/react.py` 의 action 제안 → validation → `_dispatch()`
구조는 이 파일이 존재하는지조차 모른다.

실제 엔드포인트/헤더/요청 바디/SSE 이벤트 스키마는 전부 실제 계정으로 라이브
검증했다(2026-08-26, 아래 각 함수 docstring 참고) — 추측 구현이 아니다. 단,
이 엔드포인트는 공개 개발자 API 계약이 아니라 ChatGPT 웹/Codex CLI 가 쓰는
내부 백엔드다 — 언제든 예고 없이 바뀔 수 있다(§ 결과 보고의 "제한사항" 참고).
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from finredteam.config import LLM_MAX_RETRIES, LLM_REQUEST_TIMEOUT
from finredteam.llm.base import LLMError, LLMEvent, LLMEventKind, LLMTraceHook, Usage
from finredteam.llm.codex_auth import (
    CodexAuthFileNotFound,
    CodexCredential,
    CodexCredentialInvalid,
    DEFAULT_AUTH_PATH,
    UnsupportedCodexAuthMode,
    load_codex_credential,
)

logger = logging.getLogger(__name__)

SchemaT = TypeVar("SchemaT", bound=BaseModel)

# 실측 확인(2026-08-26, 실제 계정으로 라이브 호출): 이 경로가 실제로 200 을
# 반환하고 표준 OpenAI Responses API 스트리밍 이벤트를 낸다. api.openai.com
# 이 아니다 — 그쪽으로 보내면(OAuth scope 에 추론 권한이 없어) 막힌다.
_CHATGPT_BACKEND_URL = "https://chatgpt.com/backend-api/codex/responses"


class ChatGPTAuthenticationError(LLMError):
    """401/403 — 인증정보 자체가 거부됨(만료 재시도 이후에도)."""


class ReauthenticationRequired(ChatGPTAuthenticationError):
    """auth.json 을 다시 읽어도 401 — `codex login` 재실행이 필요하다."""


class ChatGPTRateLimitError(LLMError):
    """429."""


class ChatGPTQuotaExceededError(LLMError):
    """구독 사용량 한도 초과로 보이는 응답(429 와 구분되는 quota 계열 본문)."""


class ChatGPTBackendError(LLMError):
    """그 외 5xx/네트워크/타임아웃 — 우리 코드 문제가 아니라 백엔드/전송 문제."""


class StructuredOutputError(Exception):
    """text.format(json_schema) 응답이 JSON 자체가 아님 — `anthropic_.py` 와
    같은 이유로 예외로 던지지 않고 내부적으로만 기록한다(파싱 실패는 정상
    범주, complete_json 은 빈 리스트를 돌려준다)."""


class SchemaValidationError(Exception):
    """JSON 은 맞는데 Pydantic 스키마 검증에 실패함 — 위와 같은 이유로 내부 기록용."""


def _strictify_schema(schema: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """OpenAI 구조화 출력의 `strict: true` 모드는 (실측 확인) 모든 object 마다
    ①`additionalProperties: false` 가 있어야 하고 ②`properties` 의 키 전부가
    `required` 에 들어있어야 한다 — pydantic 의 `model_json_schema()` 는 기본값이
    있는 필드를 `required` 에서 빼버려서(Optional 필드 전부 포함) 이 조건을
    자동으로 못 만족한다. `$defs`(중첩 모델)까지 재귀적으로 고친다.

    ★ 실측으로 드러난 한계: `ReActStep.action_input: dict = {}` 같은 "임의 키를
    받는 dict" 필드는 `properties` 가 없는 `{"type": "object"}` 로 나오는데,
    strict 모드는 이런 자유 형식 object 자체를 표현할 방법이 없다
    (`additionalProperties` 를 `false` 로 강제하면서 동시에 `properties` 없이
    임의 키를 허용할 수는 없다). 이런 필드가 하나라도 있으면 이 스키마 전체는
    strict 모드를 못 쓴다 — `(schema, strict 가능 여부)` 를 같이 돌려주고,
    호출부가 `text.format.strict` 를 그에 맞게 정한다(§12 와 같은 종류의
    backend 호환성 문제라 Protocol 은 그대로 둔다)."""
    strict_ok = True

    def _fix(node: Any) -> Any:
        nonlocal strict_ok
        if isinstance(node, dict):
            node = dict(node)
            if node.get("type") == "object":
                if "properties" in node:
                    node["additionalProperties"] = False
                    node["required"] = list(node["properties"].keys())
                    node["properties"] = {k: _fix(v) for k, v in node["properties"].items()}
                else:
                    strict_ok = False  # 자유 형식 dict — strict 모드로는 표현 불가
            for key in ("items", "additionalProperties"):
                if key in node and isinstance(node[key], dict):
                    node[key] = _fix(node[key])
            if "$defs" in node:
                node["$defs"] = {k: _fix(v) for k, v in node["$defs"].items()}
            if "anyOf" in node:
                node["anyOf"] = [_fix(v) for v in node["anyOf"]]
            return node
        return node

    fixed = _fix(schema)
    return fixed, strict_ok


def _wrapped_schema(schema: type[BaseModel]) -> tuple[dict[str, Any], bool]:
    """`complete_json` 은 `list[SchemaT]` 를 돌려줘야 하는데 구조화 출력은 객체
    하나만 받으므로 `{"items": [...]}` 로 감싼다(§9). 반환하는 bool 은 이
    스키마가 strict 모드로 표현 가능한지(자유 형식 dict 필드가 없는지)다.

    ★ 실측 버그(2026-08-26, `EngagementDraft`처럼 중첩 모델이 있는 스키마로
    Engagement Setup 인터뷰를 실제 실행했을 때 처음 드러남 — 그전엔 재사용
    경로만 타서 이 스키마로 LLM을 호출한 적이 없었다): `model_json_schema()`
    는 중첩 모델을 `$defs`에 모아두고 `$ref: "#/$defs/X"`로 참조하는데, 이
    `$ref`는 항상 **문서 루트** 기준으로 풀린다. `$defs`를 그대로 둔 채
    `inner`를 `properties.items.items` 밑에 감싸버리면 `$defs`가 루트가 아니라
    `items.items.$defs`에 들어가서, 백엔드가 "#/$defs/X를 찾을 수 없다"고
    거부한다(실측 에러: `chatgpt.com` 400 invalid_json_schema). `$defs`를
    감싸기 전에 루트로 끌어올려야 한다 — 중첩 깊이와 무관하게 `model_json_
    schema()`는 항상 `$defs`를 자기 스키마의 최상위 한 곳에만 둔다."""
    inner_raw = schema.model_json_schema()
    inner_defs = inner_raw.pop("$defs", None)
    inner, inner_strict_ok = _strictify_schema(inner_raw)
    wrapped_raw: dict[str, Any] = {
        "type": "object",
        "properties": {"items": {"type": "array", "items": inner}},
        "required": ["items"],
        "additionalProperties": False,
    }
    if inner_defs:
        wrapped_raw["$defs"] = inner_defs
    wrapped, outer_strict_ok = _strictify_schema(wrapped_raw)
    return wrapped, inner_strict_ok and outer_strict_ok


class _Unauthorized(Exception):
    pass


class _RateLimited(Exception):
    def __init__(self, body: str) -> None:
        self.body = body


class _BackendHttpError(Exception):
    def __init__(self, status: int, body: str) -> None:
        self.status = status
        self.body = body


def _unwrap_items(payload: Any) -> list[Any] | None:
    """`{"items": [...]}` 로 감쌌는데도(§9) **실측 확인된 실제 동작**: 이
    스키마가 strict 모드를 못 쓰는 경우(`ReActStep.action_input` 처럼 자유
    형식 dict 필드가 있으면 항상 이 상황) — non-strict json_schema 모드는
    스키마를 강제하지 않는 "권고"일 뿐이라, 모델이 `items` 래퍼를 무시하고
    안쪽 객체 하나를 그냥 바로 돌려주는 일이 실제로 일어난다(라이브 실행 중
    관측). 세 형태를 모두 허용한다: `{"items": [...]}`, 배열 자체, 객체 하나."""
    if isinstance(payload, dict) and isinstance(payload.get("items"), list):
        return payload["items"]
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        return [payload]
    return None


def _extract_final_text(events: list[tuple[str, dict[str, Any]]]) -> tuple[str, dict[str, int]]:
    """실측 확인된 이벤트 순서(2026-08-26 라이브 호출 기준):

    `response.created` → `response.in_progress` → `response.output_item.added`
    → `response.content_part.added` → `response.output_text.delta`(N개)
    → `response.output_text.done`(이 content part 의 완성된 text 포함)
    → `response.output_item.done`(같은 text 를 item.content[].text 로도 포함)
    → `response.content_part.done` → `response.completed`(response.output 은
    실측 결과 빈 배열 — 텍스트는 여기 없다, usage 만 여기 있다).

    그래서 최종 텍스트는 `output_text.done`/`output_item.done` 에서 가져오고
    (있는 걸 우선), 그마저 없으면 delta 누적을 fallback 으로 쓴다. `phase`
    필드가 `"final_answer"` 인 assistant message 만 대상으로 한다 — reasoning
    같은 다른 phase 의 텍스트를 답으로 오인하지 않기 위해서다.
    """
    delta_buffer: list[str] = []
    done_text: str | None = None
    usage: dict[str, int] = {}

    for event_type, payload in events:
        if event_type == "response.output_text.delta":
            delta = payload.get("delta")
            if isinstance(delta, str):
                delta_buffer.append(delta)
        elif event_type == "response.output_text.done":
            text = payload.get("text")
            if isinstance(text, str):
                done_text = text
        elif event_type == "response.output_item.done":
            item = payload.get("item") or {}
            if item.get("role") == "assistant" and item.get("phase", "final_answer") == "final_answer":
                for part in item.get("content") or []:
                    if part.get("type") == "output_text" and isinstance(part.get("text"), str):
                        done_text = part["text"]
        elif event_type == "response.completed":
            response = payload.get("response") or {}
            raw_usage = response.get("usage") or {}
            usage = {
                "input_tokens": raw_usage.get("input_tokens", 0) or 0,
                "output_tokens": raw_usage.get("output_tokens", 0) or 0,
            }

    final_text = done_text if done_text is not None else "".join(delta_buffer)
    return final_text, usage


class ChatGPTSubscriptionClient:
    provider = "chatgpt_subscription"

    def __init__(
        self,
        model: str,
        auth_path: Path = DEFAULT_AUTH_PATH,
        max_tokens: int = 4096,
        trace_hook: LLMTraceHook | None = None,
        _transport: httpx.BaseTransport | None = None,
    ) -> None:
        """생성 시점에 `auth.json` 을 한 번 읽는다 — 없거나 잘못돼 있으면
        (`CodexAuthFileNotFound`/`UnsupportedCodexAuthMode`/`CodexCredentialInvalid`)
        여기서 바로 실패한다. 이건 API 키가 없을 때 `build_llm_client` 가
        `RuntimeError` 를 던지는 것과 같은 자리 — 실행 시작 시점의 설정 오류지,
        `complete_json` 호출 중의 일시적 실패(`LLMError` 계열)가 아니다.

        `_transport`: 테스트 전용 — `httpx.MockTransport` 를 주입해서 실제
        네트워크 없이 SSE 응답을 시뮬레이션한다(`tests/test_chatgpt_subscription.py`)."""
        self.model = model
        self._auth_path = auth_path
        self._max_tokens = max_tokens
        self._trace_hook = trace_hook
        self._usage = Usage()
        self._credential: CodexCredential = load_codex_credential(auth_path)
        self._client = httpx.Client(timeout=LLM_REQUEST_TIMEOUT, transport=_transport)

    def set_trace_hook(self, trace_hook: LLMTraceHook | None) -> None:
        self._trace_hook = trace_hook

    def _emit(self, **fields: object) -> None:
        if self._trace_hook is None:
            return
        self._trace_hook(LLMEvent(provider=self.provider, model=self.model, **fields))

    def _build_body(self, system: str, user: str, schema: type[BaseModel], temperature: float) -> dict[str, Any]:
        # ★ 실측 확인: 이 백엔드는 'temperature' 를 최상위 파라미터로 받지 않는다
        # (400 "Unsupported parameter: temperature") — anthropic_.py 가 신형 SDK에서
        # 겪은 것과 같은 종류의 "모델/백엔드가 이 파라미터를 안 받는" 상황이다.
        # Protocol 은 그대로 두고(§12) 여기서만 조용히 무시한다.
        if temperature is not None:
            logger.debug("ChatGPT Codex backend 는 temperature 를 지원하지 않음 — 요청에서 생략")

        json_schema, strict_ok = _wrapped_schema(schema)
        if not strict_ok:
            logger.debug(
                "%s 스키마에 자유 형식 dict 필드가 있어 strict 모드를 못 씀 — "
                "일반 json_schema 모드로 전송",
                schema.__name__,
            )
        return {
            "model": self.model,
            "instructions": system,
            "input": [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": user}]}],
            "stream": True,
            "store": False,
            "text": {
                "format": {
                    "name": "agent_action",
                    "type": "json_schema",
                    "strict": strict_ok,
                    "schema": json_schema,
                }
            },
        }

    def _headers(self, credential: CodexCredential) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {credential.access_token}",
            "ChatGPT-Account-Id": credential.account_id,
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }

    def _post_and_collect(self, body: dict[str, Any], credential: CodexCredential) -> tuple[str, dict[str, int]]:
        """SSE 를 끝까지 모아서 최종 텍스트만 돌려준다 — 호출부(complete_json)는
        streaming 이 있었다는 것 자체를 모른다(§10)."""
        events: list[tuple[str, dict[str, Any]]] = []
        try:
            with self._client.stream(
                "POST", _CHATGPT_BACKEND_URL, json=body, headers=self._headers(credential)
            ) as resp:
                if resp.status_code == 401:
                    resp.read()
                    raise _Unauthorized()
                if resp.status_code == 429:
                    resp.read()
                    raise _RateLimited(resp.text[:500])
                if resp.status_code >= 400:
                    resp.read()
                    raise _BackendHttpError(resp.status_code, resp.text[:500])

                current_event: str | None = None
                for line in resp.iter_lines():
                    if line == "":
                        current_event = None
                        continue
                    if line.startswith("event:"):
                        current_event = line[len("event:") :].strip()
                        continue
                    if line.startswith("data:"):
                        raw = line[len("data:") :].strip()
                        if not raw or raw == "[DONE]" or current_event is None:
                            continue
                        try:
                            payload = json.loads(raw)
                        except json.JSONDecodeError:
                            continue
                        events.append((current_event, payload))
        except httpx.TimeoutException as exc:
            raise _BackendHttpError(0, f"timeout: {exc}") from exc
        except httpx.HTTPError as exc:
            raise _BackendHttpError(0, f"network error: {exc}") from exc

        return _extract_final_text(events)

    def complete_json(
        self, system: str, user: str, schema: type[SchemaT], temperature: float = 0.3
    ) -> list[SchemaT]:
        body = self._build_body(system, user, schema, temperature)
        self._emit(kind=LLMEventKind.CALL_STARTED, system_prompt=system, user_prompt=user)
        started_at = time.monotonic()
        credential = self._credential

        for attempt in range(1, 3):
            try:
                text, usage_dict = self._post_and_collect(body, credential)
            except _Unauthorized:
                if attempt == 1:
                    logger.info("ChatGPT 인증 만료로 추정됨 — auth.json 재로딩 후 1회 재시도")
                    try:
                        credential = load_codex_credential(self._auth_path)
                    except (CodexAuthFileNotFound, CodexCredentialInvalid, UnsupportedCodexAuthMode) as reload_exc:
                        latency_ms = (time.monotonic() - started_at) * 1000
                        self._emit(kind=LLMEventKind.CALL_FINISHED, attempt=attempt, error="reauth_required", latency_ms=latency_ms)
                        raise ReauthenticationRequired("codex login 을 다시 실행해야 함 (auth.json 재로딩 실패)") from reload_exc
                    self._credential = credential
                    continue
                latency_ms = (time.monotonic() - started_at) * 1000
                self._emit(kind=LLMEventKind.CALL_FINISHED, attempt=attempt, error="reauth_required", latency_ms=latency_ms)
                raise ReauthenticationRequired("codex login 을 다시 실행해야 함 (재인증 후에도 401)")
            except _RateLimited as exc:
                latency_ms = (time.monotonic() - started_at) * 1000
                self._emit(kind=LLMEventKind.CALL_FINISHED, attempt=attempt, error="rate_limited", latency_ms=latency_ms)
                if "quota" in exc.body.lower():
                    raise ChatGPTQuotaExceededError(f"ChatGPT 구독 사용량 초과: {exc.body}") from exc
                raise ChatGPTRateLimitError(f"ChatGPT 백엔드 429: {exc.body}") from exc
            except _BackendHttpError as exc:
                latency_ms = (time.monotonic() - started_at) * 1000
                self._emit(kind=LLMEventKind.CALL_FINISHED, attempt=attempt, error=f"http_{exc.status}", latency_ms=latency_ms)
                if exc.status in (401, 403):
                    raise ChatGPTAuthenticationError(f"ChatGPT 인증 실패({exc.status}): {exc.body}") from exc
                raise ChatGPTBackendError(f"ChatGPT 백엔드 오류({exc.status}): {exc.body}") from exc

            self._usage = self._usage + Usage(
                tokens_in=usage_dict.get("input_tokens", 0), tokens_out=usage_dict.get("output_tokens", 0), requests=1
            )

            parsed: list[SchemaT] = []
            parse_error: str | None = None
            try:
                payload = json.loads(text)
                items = _unwrap_items(payload)
            except json.JSONDecodeError as exc:
                parse_error = str(StructuredOutputError(f"구조화 출력 파싱 실패: {exc}"))
                items = None
            if parse_error is None and items is None:
                parse_error = str(StructuredOutputError(f"'items' 를 못 찾음 (payload 형태: {type(payload).__name__})"))
            if parse_error is None:
                try:
                    parsed = [schema.model_validate(item) for item in items]
                except ValidationError as exc:
                    parsed = []
                    parse_error = str(SchemaValidationError(str(exc)))

            self._emit(
                kind=LLMEventKind.ATTEMPT_RESPONSE,
                attempt=attempt,
                raw_response=text[:2000],
                parsed_ok=parse_error is None,
                parse_error=parse_error,
                tokens_in=usage_dict.get("input_tokens"),
                tokens_out=usage_dict.get("output_tokens"),
            )
            logger.debug("ChatGPT 응답 (attempt=%d): %s", attempt, text[:2000])
            if parse_error is not None:
                logger.warning("ChatGPT 응답 파싱/검증 실패 (attempt=%d): %s", attempt, parse_error)
                continue

            self._emit(
                kind=LLMEventKind.CALL_FINISHED, attempt=attempt, parsed_ok=True,
                latency_ms=(time.monotonic() - started_at) * 1000,
            )
            return parsed

        self._emit(
            kind=LLMEventKind.CALL_FINISHED, attempt=2, parsed_ok=False,
            latency_ms=(time.monotonic() - started_at) * 1000,
        )
        return []

    @property
    def usage(self) -> Usage:
        return self._usage
