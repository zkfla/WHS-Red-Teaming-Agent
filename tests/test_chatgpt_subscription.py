"""ChatGPTSubscriptionClient — 실제 네트워크 없이 `httpx.MockTransport` 로
SSE 응답을 시뮬레이션한다. 이벤트 이름/순서/필드는 실제 계정으로 라이브 검증한
것을 그대로 재현한다(2026-08-26, `chatgpt.com/backend-api/codex/responses`) —
추측 fixture 가 아니다."""
from __future__ import annotations

import json

import httpx
import pytest
from pydantic import BaseModel

from finredteam.llm.base import LLMError
from finredteam.llm.chatgpt_subscription import (
    ChatGPTAuthenticationError,
    ChatGPTBackendError,
    ChatGPTQuotaExceededError,
    ChatGPTRateLimitError,
    ChatGPTSubscriptionClient,
    ReauthenticationRequired,
    _strictify_schema,
    _unwrap_items,
    _wrapped_schema,
)


class _Action(BaseModel):
    thought: str
    action: str


class _ActionWithFreeformDict(BaseModel):
    thought: str
    action_input: dict = {}


def _sse(events: list[tuple[str, dict]]) -> bytes:
    lines = []
    for event, payload in events:
        lines.append(f"event: {event}")
        lines.append(f"data: {json.dumps(payload)}")
        lines.append("")
    return ("\n".join(lines) + "\n").encode("utf-8")


def _completed_event_stream(final_text: str, input_tokens=10, output_tokens=5) -> bytes:
    """실측 확인된 실제 이벤트 순서를 그대로 재현 — output_text.delta 여러 개 +
    output_text.done(완성 text) + output_item.done + completed(usage만, output=[])."""
    return _sse(
        [
            ("response.created", {"type": "response.created", "response": {"id": "resp_1"}}),
            ("response.in_progress", {"type": "response.in_progress"}),
            (
                "response.output_item.added",
                {"type": "response.output_item.added", "item": {"id": "msg_1", "type": "message", "role": "assistant"}},
            ),
            ("response.content_part.added", {"type": "response.content_part.added"}),
            ("response.output_text.delta", {"type": "response.output_text.delta", "delta": final_text[: len(final_text) // 2]}),
            ("response.output_text.delta", {"type": "response.output_text.delta", "delta": final_text[len(final_text) // 2 :]}),
            ("response.output_text.done", {"type": "response.output_text.done", "text": final_text}),
            (
                "response.output_item.done",
                {
                    "type": "response.output_item.done",
                    "item": {
                        "id": "msg_1",
                        "type": "message",
                        "role": "assistant",
                        "phase": "final_answer",
                        "content": [{"type": "output_text", "text": final_text}],
                    },
                },
            ),
            ("response.content_part.done", {"type": "response.content_part.done"}),
            (
                "response.completed",
                {
                    "type": "response.completed",
                    "response": {"output": [], "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens}},
                },
            ),
        ]
    )


def _make_client(tmp_path, handler) -> ChatGPTSubscriptionClient:
    auth_path = tmp_path / "auth.json"
    auth_path.write_text(
        json.dumps({"auth_mode": "chatgpt", "tokens": {"access_token": "at-1", "account_id": "acc-1"}}),
        encoding="utf-8",
    )
    transport = httpx.MockTransport(handler)
    return ChatGPTSubscriptionClient(model="gpt-5.4", auth_path=auth_path, _transport=transport)


# ── 스키마 strict 변환 ──────────────────────────────────────────────


def test_strictify_forces_all_properties_required():
    schema, strict_ok = _strictify_schema(_Action.model_json_schema())
    assert strict_ok is True
    assert set(schema["required"]) == {"thought", "action"}
    assert schema["additionalProperties"] is False


def test_wrapped_schema_detects_freeform_dict_as_not_strict_compatible():
    _, strict_ok = _wrapped_schema(_ActionWithFreeformDict)
    assert strict_ok is False  # action_input: dict 는 strict 모드로 표현 불가(실측 확인)


def test_wrapped_schema_plain_model_is_strict_compatible():
    _, strict_ok = _wrapped_schema(_Action)
    assert strict_ok is True


class _NestedCredential(BaseModel):
    username: str
    password: str


class _NestedDraft(BaseModel):
    target_name: str | None = None
    credentials: list[_NestedCredential] | None = None


class _ClarificationLike(BaseModel):
    """`engagement/setup.py::ClarificationTurn`(updated_fields: EngagementDraft)의
    중첩 구조를 그대로 흉내낸다 — 실측 버그 재현용."""

    updated_fields: _NestedDraft
    next_question: str | None = None


def _collect_defs_refs(node: object) -> set[str]:
    """스키마 트리를 순회하며 `#/$defs/X` 참조가 가리키는 이름을 전부 모은다."""
    refs: set[str] = set()
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/$defs/"):
            refs.add(ref[len("#/$defs/") :])
        for value in node.values():
            refs |= _collect_defs_refs(value)
    elif isinstance(node, list):
        for item in node:
            refs |= _collect_defs_refs(item)
    return refs


def test_wrapped_schema_hoists_nested_defs_to_document_root():
    """★ 실측 버그 회귀 가드(2026-08-26): 중첩 모델(EngagementDraft 안의
    EngagementCredential처럼)이 있는 스키마로 실제 Engagement Setup 인터뷰를
    돌렸을 때 chatgpt.com 백엔드가 400(invalid_json_schema, "reference to
    component '#/$defs/EngagementDraft' which was not found in the schema")
    을 반환했다 — `$defs`가 문서 루트가 아니라 `properties.items.items` 밑에
    남아있어서 `$ref: "#/$defs/..."`가 안 풀렸기 때문이다. `$defs`는 항상
    래핑된 스키마의 최상위에 있어야 하고, 스키마 안의 모든 `$ref`가 실제로
    그 최상위 `$defs`에서 풀려야 한다."""
    schema, _ = _wrapped_schema(_ClarificationLike)

    assert "$defs" in schema  # 루트에 있어야 함
    assert "$defs" not in schema["properties"]["items"]["items"]  # 안쪽에 남으면 안 됨

    refs = _collect_defs_refs(schema)
    assert refs  # 이 스키마엔 실제로 $ref 가 있어야 의미 있는 테스트다
    assert refs <= set(schema["$defs"])  # 모든 참조가 루트 $defs 에서 실제로 풀려야 함


# ── items 래퍼 방어적 해석 (실측 확인된 버그: non-strict 모드에서 모델이
# {"items":[...]} 래퍼를 무시하고 객체를 바로 돌려줄 때가 있다) ──────


def test_unwrap_items_handles_proper_wrapper():
    assert _unwrap_items({"items": [{"a": 1}]}) == [{"a": 1}]


def test_unwrap_items_handles_bare_array():
    assert _unwrap_items([{"a": 1}]) == [{"a": 1}]


def test_unwrap_items_handles_bare_object_wraps_as_single_item():
    assert _unwrap_items({"a": 1}) == [{"a": 1}]


def test_unwrap_items_returns_none_for_scalar():
    assert _unwrap_items("not an object") is None


def test_complete_json_recovers_when_model_ignores_items_wrapper(tmp_path):
    # 실측 확인된 실제 버그 재현: strict 모드를 못 쓰는 스키마(ReActStep 처럼
    # action_input: dict 필드가 있는 경우)에서 모델이 {"items":[...]} 래퍼 없이
    # 안쪽 객체를 바로 반환했다 — 이제는 이걸 [payload] 로 복구해야 한다.
    bare_object = json.dumps({"thought": "hi", "action_input": {"x": 1}})

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_completed_event_stream(bare_object))

    client = _make_client(tmp_path, handler)
    results = client.complete_json("sys", "user", _ActionWithFreeformDict)

    assert results == [_ActionWithFreeformDict(thought="hi", action_input={"x": 1})]


# ── 정상 응답 ──────────────────────────────────────────────────────


def test_complete_json_returns_parsed_pydantic_objects(tmp_path):
    final_text = json.dumps({"items": [{"thought": "hi", "action": "finish"}]})

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_completed_event_stream(final_text, 42, 7))

    client = _make_client(tmp_path, handler)
    results = client.complete_json("sys", "user", _Action, temperature=0.2)

    assert results == [_Action(thought="hi", action="finish")]
    assert client.usage.tokens_in == 42
    assert client.usage.tokens_out == 7


def test_complete_json_omits_temperature_from_request_body(tmp_path):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        final_text = json.dumps({"items": [{"thought": "hi", "action": "finish"}]})
        return httpx.Response(200, content=_completed_event_stream(final_text))

    client = _make_client(tmp_path, handler)
    client.complete_json("sys", "user", _Action, temperature=0.7)

    assert "temperature" not in captured["body"]


def test_complete_json_sends_bearer_and_account_id_headers(tmp_path):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = request.headers
        final_text = json.dumps({"items": []})
        return httpx.Response(200, content=_completed_event_stream(final_text))

    client = _make_client(tmp_path, handler)
    client.complete_json("sys", "user", _Action)

    assert captured["headers"]["authorization"] == "Bearer at-1"
    assert captured["headers"]["chatgpt-account-id"] == "acc-1"


# ── 구조화 출력 실패(정상 범주 — 예외 아님) ──────────────────────────


def test_malformed_json_returns_empty_list_not_exception(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_completed_event_stream("not valid json"))

    client = _make_client(tmp_path, handler)
    assert client.complete_json("sys", "user", _Action) == []


def test_schema_validation_failure_returns_empty_list(tmp_path):
    bad = json.dumps({"items": [{"thought": "hi"}]})  # action 필드 누락

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_completed_event_stream(bad))

    client = _make_client(tmp_path, handler)
    assert client.complete_json("sys", "user", _Action) == []


def test_empty_items_returns_empty_list(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_completed_event_stream(json.dumps({"items": []})))

    client = _make_client(tmp_path, handler)
    assert client.complete_json("sys", "user", _Action) == []


# ── 인증/재시도 ────────────────────────────────────────────────────


def test_401_reloads_auth_json_and_retries_once(tmp_path):
    auth_path = tmp_path / "auth.json"
    auth_path.write_text(
        json.dumps({"auth_mode": "chatgpt", "tokens": {"access_token": "stale-token", "account_id": "acc-1"}}),
        encoding="utf-8",
    )
    calls = {"n": 0}
    headers_seen: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        headers_seen.append(request.headers.get("authorization"))
        calls["n"] += 1
        if calls["n"] == 1:
            # 401 이 온 뒤, 클라이언트가 재로딩하기 전에 파일을 실제로 갱신해둔다 —
            # "재로딩이 진짜로 일어나는지"(캐시된 값을 계속 쓰지 않는지)를 검증한다.
            auth_path.write_text(
                json.dumps({"auth_mode": "chatgpt", "tokens": {"access_token": "fresh-token", "account_id": "acc-1"}}),
                encoding="utf-8",
            )
            return httpx.Response(401, json={"error": {"code": "token_expired"}})
        return httpx.Response(
            200, content=_completed_event_stream(json.dumps({"items": [{"thought": "ok", "action": "finish"}]}))
        )

    transport = httpx.MockTransport(handler)
    client = ChatGPTSubscriptionClient(model="gpt-5.4", auth_path=auth_path, _transport=transport)
    results = client.complete_json("sys", "user", _Action)

    assert results == [_Action(thought="ok", action="finish")]
    assert headers_seen == ["Bearer stale-token", "Bearer fresh-token"]


def test_401_twice_raises_reauthentication_required(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"code": "token_expired"}})

    client = _make_client(tmp_path, handler)
    with pytest.raises(ReauthenticationRequired):
        client.complete_json("sys", "user", _Action)


def test_reauthentication_required_is_an_llm_error(tmp_path):
    # agent/react.py 는 LLMError 만 잡는다 — 이 provider 의 인증 실패도 그
    # 계약을 반드시 만족해야 기존 backoff/llm_unavailable 처리를 그대로 탄다.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"code": "token_expired"}})

    client = _make_client(tmp_path, handler)
    with pytest.raises(LLMError):
        client.complete_json("sys", "user", _Action)


# ── 그 외 HTTP 오류 ────────────────────────────────────────────────


def test_429_raises_rate_limit_error(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, text='{"error":{"type":"rate_limit_error","message":"Error"}}')

    client = _make_client(tmp_path, handler)
    with pytest.raises(ChatGPTRateLimitError):
        client.complete_json("sys", "user", _Action)


def test_429_with_quota_wording_raises_quota_exceeded(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, text='{"error":"subscription quota exceeded"}')

    client = _make_client(tmp_path, handler)
    with pytest.raises(ChatGPTQuotaExceededError):
        client.complete_json("sys", "user", _Action)


def test_5xx_raises_backend_error(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="internal error")

    client = _make_client(tmp_path, handler)
    with pytest.raises(ChatGPTBackendError):
        client.complete_json("sys", "user", _Action)


def test_403_raises_authentication_error(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="forbidden")

    client = _make_client(tmp_path, handler)
    with pytest.raises(ChatGPTAuthenticationError):
        client.complete_json("sys", "user", _Action)


def test_network_error_raises_backend_error(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    client = _make_client(tmp_path, handler)
    with pytest.raises(ChatGPTBackendError):
        client.complete_json("sys", "user", _Action)


def test_400_model_not_supported_raises_backend_error_not_silent(tmp_path):
    # 실측 확인: 지원 안 하는 모델을 쓰면 400 이 온다 — 조용히 삼키지 않는다.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"detail": "The 'gpt-5' model is not supported when using Codex with a ChatGPT account."})

    client = _make_client(tmp_path, handler)
    with pytest.raises(ChatGPTBackendError):
        client.complete_json("sys", "user", _Action)
