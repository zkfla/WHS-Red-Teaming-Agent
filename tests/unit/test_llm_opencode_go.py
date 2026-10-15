"""OpenCode Go adapter wire tests (offline, deterministic).

Transport isolation: every request transport is intercepted by patching
httpx.AsyncClient.send globally. Patching the SDK session object does NOT
intercept every transport, so it is never used here. Only synthetic key
values are used; no test performs real network I/O.

SDK optionality: litellm is optional in the default dev install. Wire tests
take the _sdk fixture, which runs pytest.importorskip for litellm so they
skip cleanly when the SDK is absent (the dedicated SDK CI job runs an
explicit import preflight first, so the skip cannot hide a missing SDK
there). The SDK-free cases below (grammar, fingerprint, version floor,
poison import) run in both jobs. LITELLM_LOCAL_MODEL_COST_MAP is forced
before any SDK import so the import never fetches the remote cost catalog.
"""
from __future__ import annotations

import os

os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
import asyncio
import hashlib
import json
import sys
from importlib import metadata as _metadata

import httpx
import pytest

from redteam.core.config import LLMConfig
from redteam.core.ports.llm import (
    NATIVE_PROTOCOL_MISSING_CALL_ID,
    NATIVE_PROTOCOL_MISSING_TOOL_NAME,
    LLMProviderFailure,
    NativeToolSpec,
)
from redteam.core.request_scope import llm_request_scope
from redteam.llm import opencode_go as go
from redteam.llm.opencode_go_config import (
    OPENCODE_GO_PROVIDER,
    USER_AGENT,
    llm_connection_fingerprint_for_config,
    parse_model,
    validate_model,
)

# Set before the first pytest.importorskip("litellm") below so the SDK import
# never fetches the remote model-cost catalog. Every redteam import above is
# SDK-free, so no litellm import can happen before this line executes.
os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
_SYNTH_KEY = "sk-SYNTHETIC-OPENCODE-GO-KEY-9f8e7d"
_KEY_ENV = "OPENCODE_GO_TEST_KEY"
CHAT_URL = "https://opencode.ai/zen/go/v1/chat/completions"
RESPONSES_URL = "https://opencode.ai/zen/go/v1/responses"
MESSAGES_URL = "https://opencode.ai/zen/go/v1/messages"
FORBIDDEN_BODY_KEYS = frozenset(
    {
        "auth_token",
        "use_bearer_for_custom_base",
        "use_responses_api",
        "api_base",
        "base_url",
        "headers",
        "extra_headers",
        "custom_llm_provider",
        "client",
        "http_client",
        "api_key",
        "num_retries",
    }
)
CAPTURED: list = []
ERROR_PLAN: dict = {}
def _chat_payload(body, *, content="hello-chat", total=5, tool_calls=None):
    message = {"role": "assistant", "content": content}
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    return {
        "id": "chatcmpl-test", "object": "chat.completion", "created": 1,
        "model": "test-model",
        "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": total},
    }
def _responses_payload(body, *, text="hello-resp", total=9,
                        status="completed", output=None):
    if output is None:
        output = [{"type": "message", "id": "msg-1", "status": "completed",
                   "role": "assistant",
                   "content": [{"type": "output_text", "text": text,
                                "annotations": []}]}]
    return {
        "id": "resp-test", "object": "response", "created_at": 1,
        "status": status, "model": "test-model", "output": output,
        "usage": {"input_tokens": 2, "output_tokens": 3, "total_tokens": total},
    }
def _messages_payload(body, *, text="hello-msg"):
    return {
        "id": "msg-test", "type": "message", "role": "assistant",
        "model": "test-model",
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 2, "output_tokens": 3},
    }
RESPONDERS: dict = {
    "chat": lambda body: _chat_payload(body),
    "responses": lambda body: _responses_payload(body),
    "messages": lambda body: _messages_payload(body),
}
async def _fake_send(self, request, **kwargs):
    url = str(request.url)
    try:
        body = json.loads(request.content.decode() or "null")
    except Exception:
        body = {"_unparseable": repr(request.content[:200])}
    CAPTURED.append({"url": url, "headers": dict(request.headers), "body": body})
    if url.endswith("/chat/completions"):
        path = "chat"
    elif url.endswith("/responses"):
        path = "responses"
    elif url.endswith("/messages"):
        path = "messages"
    else:
        raise AssertionError(
            "unexpected outbound URL (test must stay offline): " + url)
    if path in ERROR_PLAN:
        spec = ERROR_PLAN[path]
        return httpx.Response(
            spec["status"], json=spec["payload"],
            headers=spec.get("headers", {}), request=request)
    return httpx.Response(200, json=RESPONDERS[path](body), request=request)
@pytest.fixture(autouse=True)
def _offline_wire(monkeypatch):
    monkeypatch.setenv(_KEY_ENV, _SYNTH_KEY)
    monkeypatch.setattr(httpx.AsyncClient, "send", _fake_send)
    CAPTURED.clear()
    ERROR_PLAN.clear()
    RESPONDERS["chat"] = lambda body: _chat_payload(body)
    RESPONDERS["responses"] = lambda body: _responses_payload(body)
    RESPONDERS["messages"] = lambda body: _messages_payload(body)
    yield
@pytest.fixture
def _sdk():
    return pytest.importorskip(
        "litellm", reason="opencode-go wire tests need the litellm SDK")
def _config(model, env_var=_KEY_ENV):
    return LLMConfig(
        provider="opencode-go", default_model=model, api_key_env_var=env_var)
def _build(model):
    return go.build(_config(model))
def _last():
    assert CAPTURED, "expected at least one outbound request"
    return CAPTURED[-1]
def _headers(entry):
    return {key.lower(): value for key, value in entry["headers"].items()}
def _session_of(entry):
    return _headers(entry)["x-opencode-session"]
def _expected_session(run_id, owner):
    return hashlib.sha256(repr((run_id, owner)).encode()).hexdigest()
def _assert_clean_body(entry):
    body = entry["body"]
    assert isinstance(body, dict), "request body must be a JSON object"
    leaked = FORBIDDEN_BODY_KEYS.intersection(body.keys())
    assert not leaked, "flag leaked into request body"
    assert _SYNTH_KEY not in json.dumps(body, sort_keys=True), (
        "API key value must never appear in the request body")
def test_parse_model_routes_all_forms():
    chat = parse_model("openai/gpt-x")
    assert chat.protocol == "chat"
    assert chat.endpoint == "https://opencode.ai/zen/go/v1"
    assert chat.sdk_model == "openai/gpt-x"
    assert chat.prefix == "openai/"
    resp = parse_model("openai/responses/gpt-x")
    assert resp.protocol == "responses"
    assert resp.endpoint == "https://opencode.ai/zen/go/v1"
    assert resp.sdk_model == "openai/gpt-x"
    assert resp.prefix == "openai/responses/"
    msg = parse_model("anthropic/claude-x")
    assert msg.protocol == "messages"
    assert msg.endpoint == "https://opencode.ai/zen/go"
    assert msg.sdk_model == "anthropic/claude-x"
    assert msg.prefix == "anthropic/"
def test_parse_model_rejects_bad_shapes():
    for bad in ["openai/responses/", "openai/responses/   ", "openai/   ",
                "openai/", "openai/a/b", "openai/a b", "anthropic/",
                "anthropic/a/b", "gpt-4o", "openai", "foo/bar", "", "   ",
                None, 123]:
        with pytest.raises(ValueError, match="REDTEAM_LLM_MODEL"):
            parse_model(bad)
def test_parse_model_never_falls_through_to_shorter_prefix():
    with pytest.raises(ValueError, match="openai/responses/"):
        parse_model("openai/responses/")
def test_validate_model_strips_and_delegates():
    assert validate_model("  openai/gpt-x  ") == "openai/gpt-x"
    assert validate_model("openai/responses/gpt-x") == "openai/responses/gpt-x"
    with pytest.raises(ValueError, match="is not set"):
        validate_model("   ")
    with pytest.raises(ValueError, match="REDTEAM_LLM_MODEL"):
        validate_model("openai/a/b")
    with pytest.raises(ValueError, match="REDTEAM_LLM_MODEL"):
        validate_model("gpt-x")
def test_fingerprint_is_stable_secret_free_hex():
    first = llm_connection_fingerprint_for_config(_config("openai/gpt-fp"))
    second = llm_connection_fingerprint_for_config(_config("openai/gpt-fp"))
    assert first == second
    assert len(first) == 64
    assert all(ch in "0123456789abcdef" for ch in first)
    assert _SYNTH_KEY not in first
def test_fingerprint_varies_by_model_and_protocol():
    seen = {
        llm_connection_fingerprint_for_config(_config("openai/gpt-fp")),
        llm_connection_fingerprint_for_config(_config("openai/other")),
        llm_connection_fingerprint_for_config(_config("openai/responses/gpt-fp")),
        llm_connection_fingerprint_for_config(_config("anthropic/cl-fp")),
    }
    assert len(seen) == 4
def test_fingerprint_excludes_key_value_session_and_padding(monkeypatch):
    base = llm_connection_fingerprint_for_config(_config("openai/gpt-fp"))
    monkeypatch.setenv(_KEY_ENV, "sk-DIFFERENT-VALUE-zzz")
    assert llm_connection_fingerprint_for_config(_config("openai/gpt-fp")) == base
    assert "sk-DIFFERENT-VALUE-zzz" not in base
    assert llm_connection_fingerprint_for_config(
        _config("  openai/gpt-fp  ")) == base
    with llm_request_scope("run-fp", "owner-fp"):
        assert llm_connection_fingerprint_for_config(
            _config("openai/gpt-fp")) == base
def test_fingerprint_none_for_other_providers():
    assert llm_connection_fingerprint_for_config(
        LLMConfig(provider="litellm", default_model="gpt-4o-mini")) is None
    assert llm_connection_fingerprint_for_config(object()) is None
@pytest.mark.parametrize(
    "bad", ["1.103.9", "1.104.0a1", "1.104.0rc1", "not-a-version", ""])
def test_sdk_version_floor_rejects_unsupported(monkeypatch, bad):
    monkeypatch.setattr(go, "_litellm_version", lambda: bad)
    with pytest.raises(RuntimeError, match="requires litellm>=1.104.0"):
        go._require_litellm()
def test_sdk_version_floor_missing_dist_is_actionable(monkeypatch):
    def _missing():
        raise _metadata.PackageNotFoundError()
    monkeypatch.setattr(go, "_litellm_version", _missing)
    with pytest.raises(RuntimeError, match="upgrade with"):
        go._require_litellm()
def test_sdk_free_import_parse_and_fingerprint_without_litellm(monkeypatch):
    import importlib
    for name in ("redteam.llm.opencode_go", "redteam.llm.opencode_go_config",
                 "redteam.llm.litellm"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    monkeypatch.setitem(sys.modules, "litellm", None)
    fresh = importlib.import_module("redteam.llm.opencode_go")
    parsed = fresh.parse_model("openai/responses/gpt-x")
    assert parsed.protocol == "responses"
    assert parsed.sdk_model == "openai/gpt-x"
    fp = fresh.llm_connection_fingerprint_for_config(_config("openai/gpt-x"))
    assert fp is not None and len(fp) == 64
    with pytest.raises(Exception, match="litellm"):
        fresh.build(_config("openai/gpt-x"))
async def test_chat_exact_url_auth_and_body(_sdk):
    client = _build("openai/gpt-wire-chat")
    assert client.provider == OPENCODE_GO_PROVIDER
    assert client.protocol == "chat"
    assert client.endpoint == "https://opencode.ai/zen/go/v1"
    with llm_request_scope("run-chat", "owner-chat"):
        text = await client.complete(
            messages=[{"role": "user", "content": "hi"}], system="sys-hint")
    assert text == "hello-chat"
    entry = _last()
    assert entry["url"] == CHAT_URL
    headers = _headers(entry)
    assert headers["authorization"] == "Bearer " + _SYNTH_KEY
    assert headers["user-agent"] == USER_AGENT
    assert _session_of(entry) == _expected_session("run-chat", "owner-chat")
    body = entry["body"]
    assert body["model"] == "gpt-wire-chat"
    assert "/" not in body["model"]
    assert body["messages"][0] == {"role": "system", "content": "sys-hint"}
    _assert_clean_body(entry)

@pytest.mark.parametrize("model,path", [
    ("openai/gpt-wire-usage", "chat"),
    ("openai/responses/gpt-wire-usage", "responses"),
    ("anthropic/claude-wire-usage", "messages"),
])
async def test_request_local_cache_usage_survives_actual_sdk_transformation(_sdk, model, path):
    from redteam.core.ports.llm import LLMUsage

    def response(body):
        if path == "messages":
            payload = _messages_payload(body)
            payload["usage"] = {"input_tokens": 5, "output_tokens": 2,
                                "cache_read_input_tokens": 7, "cache_creation_input_tokens": 3}
        elif path == "responses":
            payload = _responses_payload(body)
            payload["usage"] = {"input_tokens": 15, "output_tokens": 2, "total_tokens": 17,
                                "input_tokens_details": {
                                    "cached_tokens": 7, "cache_write_tokens": 3}}
        else:
            payload = _chat_payload(body)
            payload["usage"] = {"prompt_tokens": 15, "completion_tokens": 2, "total_tokens": 17,
                                "prompt_tokens_details": {
                                    "cached_tokens": 7, "cache_write_tokens": 3}}
        return payload

    RESPONDERS[path] = response
    client = _build(model)
    with llm_request_scope("run-usage", "owner-usage"):
        result = await client.complete_with_usage(messages=[{"role": "user", "content": "hi"}])
    assert result.usage == LLMUsage(15, 2, 17, 7, 3)
    assert len(CAPTURED) == 1
    _assert_clean_body(_last())


@pytest.mark.parametrize("split", [False, True])
@pytest.mark.parametrize("model,path", [
    ("openai/gpt-wire-planner", "chat"),
    ("openai/responses/gpt-wire-planner", "responses"),
    ("anthropic/claude-wire-planner", "messages"),
])
async def test_independent_planner_prefix_survives_sdk_wire_and_shared_meter(
    _sdk, tmp_path, split, model, path,
):
    from redteam.app.agent_profile import load_specialist_profile
    from redteam.core.contracts.llm_policy import LLMRequestPolicy
    from redteam.orchestration.cognition import StructuredDecisionClient
    from redteam.orchestration.hypothesis_planner import (
        _SINGLE_PLAN_SYSTEM,
        _SPLIT_PLAN_SYSTEM,
        HypothesisDecision,
    )
    from redteam.orchestration.llm_requests import RunRequestLedger
    from redteam.orchestration.metered_llm import RunMeteredLLM
    from redteam.orchestration.probe_atomization import HypothesisPlanDraft
    from tests.integration.test_hypothesis_planner import held
    from tests.integration.test_probe_workers import PROFILE
    from tests.unit.test_orchestration_coordination import RUN, world

    def response(body):
        text = json.dumps(held("Synthetic public judgment only"))
        if path == "messages":
            payload = _messages_payload(body, text=text)
        elif path == "responses":
            payload = _responses_payload(body, text=text, total=5)
        else:
            payload = _chat_payload(body, content=text)
        return payload

    RESPONDERS[path] = response
    profile = load_specialist_profile(PROFILE if split else PROFILE.with_name(
        "native-probes-legacy.yaml"))
    w = await world(tmp_path, "sqlite")
    ledger = RunRequestLedger(w.coordinator)
    await ledger.bind(RUN, LLMRequestPolicy(mode="bounded", max_requests=2))
    client = StructuredDecisionClient(RunMeteredLLM(_build(model), ledger, prompts=profile.prompts))
    schema = (HypothesisPlanDraft if split else HypothesisDecision).model_json_schema()
    for index in range(2):
        with llm_request_scope(RUN, "planner"):
            assert await client.ask(
                stage="hypothesis_planning", payload={"input_sequence": index,
                    "worker_result": "failed" if index else "inconclusive"},
                response_schema=schema, system=_SPLIT_PLAN_SYSTEM if split else _SINGLE_PLAN_SYSTEM,
            ) == held("Synthetic public judgment only")
    assert len(CAPTURED) == 2
    first, second = [entry["body"] for entry in CAPTURED]
    assert first["model"] == second["model"]
    assert all("previous_response_id" not in body and "conversation" not in body
               and not body.get("tools") for body in (first, second))
    if path == "responses":
        assert first["instructions"] == second["instructions"]
        assert first["text"] == second["text"]
        data = [body["input"] for body in (first, second)]
    elif path == "chat":
        assert first["messages"][0] == second["messages"][0]
        assert first["messages"][0]["role"] == "system"
        assert first["response_format"] == second["response_format"]
        data = [body["messages"][1:] for body in (first, second)]
    else:
        assert first["system"] == second["system"]
        assert first["output_config"] == second["output_config"]
        data = [body["messages"] for body in (first, second)]
    assert data[0] != data[1]
    assert all(len(messages) == 1 and messages[0]["role"] == "user" for messages in data)
    assert all("Synthetic public judgment only" not in json.dumps(messages) for messages in data)
    usage = await ledger.snapshot(RUN)
    assert usage["spent"] == usage["by_stage"]["hypothesis_planning"]["spent"] == 2
    assert usage["known_tokens"] == 10 and usage["unknown_usage"] == 0

async def test_responses_exact_url_auth_and_body(_sdk):
    client = _build("openai/responses/gpt-wire-resp")
    assert client.protocol == "responses"
    assert client.endpoint == "https://opencode.ai/zen/go/v1"
    with llm_request_scope("run-resp", "owner-resp"):
        text = await client.complete(
            messages=[{"role": "user", "content": "hi"}], system="sys-hint")
    assert text == "hello-resp"
    entry = _last()
    assert entry["url"] == RESPONSES_URL
    headers = _headers(entry)
    assert headers["authorization"] == "Bearer " + _SYNTH_KEY
    assert headers["user-agent"] == USER_AGENT
    assert _session_of(entry) == _expected_session("run-resp", "owner-resp")
    body = entry["body"]
    assert body["model"] == "gpt-wire-resp"
    assert "openai/" not in body["model"]
    assert "responses/" not in body["model"]
    assert body.get("stream") is False
    assert body["instructions"] == "sys-hint"
    _assert_clean_body(entry)
async def test_messages_exact_url_auth_and_body(_sdk):
    client = _build("anthropic/claude-wire-msg")
    assert client.protocol == "messages"
    assert client.endpoint == "https://opencode.ai/zen/go"
    with llm_request_scope("run-msg", "owner-msg"):
        text = await client.complete(
            messages=[{"role": "user", "content": "hi"}], system="sys-hint")
    assert text == "hello-msg"
    entry = _last()
    assert entry["url"] == MESSAGES_URL
    headers = _headers(entry)
    assert headers["authorization"] == "Bearer " + _SYNTH_KEY
    assert headers["user-agent"] == USER_AGENT
    assert _session_of(entry) == _expected_session("run-msg", "owner-msg")
    body = entry["body"]
    assert body["model"] == "claude-wire-msg"
    assert "/" not in body["model"]
    assert body["system"] == [{"text": "sys-hint", "type": "text"}]
    assert "response_format" not in body
    assert "output_config" not in body
    _assert_clean_body(entry)
async def test_messages_dual_auth_documents_sdk_x_api_key(_sdk):
    client = _build("anthropic/claude-wire-auth")
    with llm_request_scope("run-auth", "owner-auth"):
        await client.complete(messages=[{"role": "user", "content": "hi"}])
    headers = _headers(_last())
    assert headers["authorization"] == "Bearer " + _SYNTH_KEY
    extra = headers.get("x-api-key")
    assert extra in (None, _SYNTH_KEY), (
        "the litellm Anthropic transport may co-send x-api-key with the same "
        "key to the same endpoint; the adapter owns the Bearer header and "
        "never suppresses SDK headers or uses use_bearer_for_custom_base")
async def test_session_stable_per_scope_with_client_fallback(_sdk):
    first = _build("openai/gpt-scope")
    second = _build("openai/gpt-scope")
    with llm_request_scope("run-A", "alice"):
        await first.complete(messages=[{"role": "user", "content": "hi"}])
        session_a = _session_of(_last())
        await first.complete(messages=[{"role": "user", "content": "hi"}])
        assert _session_of(_last()) == session_a
    assert session_a == _expected_session("run-A", "alice")
    with llm_request_scope("run-B", "bob"):
        await first.complete(messages=[{"role": "user", "content": "hi"}])
        assert _session_of(_last()) != session_a
    await first.complete(messages=[{"role": "user", "content": "hi"}])
    fallback = _session_of(_last())
    await first.complete(messages=[{"role": "user", "content": "hi"}])
    assert _session_of(_last()) == fallback
    await second.complete(messages=[{"role": "user", "content": "hi"}])
    assert _session_of(_last()) != fallback
async def test_parallel_scopes_keep_distinct_stable_sessions(_sdk):
    client = _build("openai/gpt-par")
    async def _one(run_id, owner):
        with llm_request_scope(run_id, owner):
            await client.complete(messages=[{"role": "user", "content": "hi"}])
    mark = len(CAPTURED)
    await asyncio.gather(_one("run-PA", "ann"), _one("run-PB", "ben"))
    fresh = CAPTURED[mark:]
    assert len(fresh) == 2
    sessions = {_session_of(entry) for entry in fresh}
    assert sessions == {_expected_session("run-PA", "ann"),
                        _expected_session("run-PB", "ben")}
    await _one("run-PA", "ann")
    assert _session_of(_last()) == _expected_session("run-PA", "ann")
SCHEMA = {"type": "object", "properties": {"answer": {"type": "string"}},
          "required": ["answer"], "additionalProperties": False}
SPEC = NativeToolSpec(name="lookup", description="look up",
                      input_schema={"type": "object",
                                    "properties": {"q": {"type": "string"}}})

async def test_chat_structured_returns_parsed_dict_with_request_usage(_sdk):
    RESPONDERS["chat"] = lambda body: _chat_payload(
        body, content=json.dumps({"answer": "ok-chat"}), total=5)
    client = _build("openai/gpt-struct")
    with llm_request_scope("run-sc", "owner-sc"):
        value = await client.complete_structured(
            messages=[{"role": "user", "content": "hi as json"}],
            response_schema=SCHEMA)
        assert value == {"answer": "ok-chat"}
        done = await client.complete_structured_with_usage(
            messages=[{"role": "user", "content": "hi as json"}],
            response_schema=SCHEMA)
        assert json.loads(done.value) == {"answer": "ok-chat"}
        assert done.total_tokens == 5
    body = _last()["body"]
    assert body["model"] == "gpt-struct"
    assert body["response_format"]["type"] == "json_schema"
    assert body["response_format"]["json_schema"]["schema"] == SCHEMA
    _assert_clean_body(_last())
    RESPONDERS["chat"] = lambda body: _chat_payload(
        body, content=json.dumps({"answer": "second"}), total=11)
    with llm_request_scope("run-sc", "owner-sc"):
        again = await client.complete_structured_with_usage(
            messages=[{"role": "user", "content": "hi as json"}],
            response_schema=SCHEMA)
    assert again.total_tokens == 11

async def test_chat_structured_without_usage_reports_sdk_default(_sdk):
    payload = _chat_payload({"marker": True})
    payload.pop("usage", None)
    RESPONDERS["chat"] = lambda body: dict(payload)
    client = _build("openai/gpt-nou")
    with llm_request_scope("run-nu", "owner-nu"):
        done = await client.complete_structured_with_usage(
            messages=[{"role": "user", "content": "hi"}], response_schema=SCHEMA)
    # The SDK backfills a zero usage object when the wire response omits
    # usage, so the adapter reports the SDK-surfaced total (0) rather than
    # unknown. Usage still travels with this request when the wire carries
    # a real total (asserted above).
    assert done.total_tokens == 0

async def test_responses_structured_single_shot_schema_and_usage(_sdk):
    RESPONDERS["responses"] = lambda body: _responses_payload(
        body, text=json.dumps({"answer": "ok-resp"}), total=9)
    client = _build("openai/responses/gpt-struct")
    with llm_request_scope("run-sr", "owner-sr"):
        value = await client.complete_structured(
            messages=[{"role": "user", "content": "hi as json"}],
            response_schema=SCHEMA)
        assert value == {"answer": "ok-resp"}
        done = await client.complete_structured_with_usage(
            messages=[{"role": "user", "content": "hi as json"}],
            response_schema=SCHEMA)
        assert done.total_tokens == 9
    body = _last()["body"]
    assert body["model"] == "gpt-struct"
    assert body.get("stream") is False
    assert body["text"] == {"format": {"type": "json_schema",
                                       "name": "structured_decision",
                                       "schema": SCHEMA, "strict": False}}
    assert json.dumps(SCHEMA) in body["instructions"]
    _assert_clean_body(_last())

async def test_messages_structured_carries_schema_via_output_config(_sdk):
    """Schema carriage, not strict provider enforcement.

    The Messages transport has no response_format contract: the SDK drops
    that field on this path, so the adapter conveys the JSON schema in
    output_config instead. This pins that carriage (the schema bytes reach
    the wire while response_format stays absent) without claiming that the
    provider validates against it.
    """
    RESPONDERS["messages"] = lambda body: _messages_payload(
        body, text=json.dumps({"answer": "ok-msg"}))
    client = _build("anthropic/claude-struct")
    with llm_request_scope("run-sm", "owner-sm"):
        value = await client.complete_structured(
            messages=[{"role": "user", "content": "hi as json"}],
            response_schema=SCHEMA)
        assert value == {"answer": "ok-msg"}
        done = await client.complete_structured_with_usage(
            messages=[{"role": "user", "content": "hi as json"}],
            response_schema=SCHEMA)
        assert done.total_tokens == 5
    body = _last()["body"]
    assert body["model"] == "claude-struct"
    assert body["output_config"] == {"format": {"type": "json_schema",
                                                  "schema": SCHEMA}}
    assert "response_format" not in body
    _assert_clean_body(_last())

@pytest.mark.parametrize("status", ["failed", "incomplete", "cancelled",
                                    "expired"])
async def test_responses_rejects_non_completed_statuses(_sdk, status):
    RESPONDERS["responses"] = lambda body: _responses_payload(body, status=status)
    client = _build("openai/responses/gpt-status")
    with llm_request_scope("run-st", "owner-st"):
        with pytest.raises(LLMProviderFailure):
            await client.complete(messages=[{"role": "user", "content": "hi"}])
        with pytest.raises(LLMProviderFailure):
            await client.complete_structured_with_usage(
                messages=[{"role": "user", "content": "hi"}],
                response_schema=SCHEMA)
        with pytest.raises(LLMProviderFailure):
            await client.complete_native([{"role": "user", "content": "hi"}], [SPEC])

async def test_chat_native_tool_call_round_trip(_sdk):
    tool_call = {"id": "call_1", "type": "function", "function": {
        "name": "lookup", "arguments": json.dumps({"q": "a"})}}
    RESPONDERS["chat"] = lambda body: _chat_payload(
        body, content="", total=6, tool_calls=[dict(tool_call)])
    client = _build("openai/gpt-tools")
    with llm_request_scope("run-tc", "owner-tc"):
        first = await client.complete_native([{"role": "user", "content": "go"}],
                                             [SPEC])
    assert first.protocol_error is None
    assert len(first.tool_calls) == 1
    assert first.tool_calls[0].call_id == "call_1"
    assert first.tool_calls[0].name == "lookup"
    assert first.tool_calls[0].arguments == {"q": "a"}
    assert first.total_tokens == 6
    turn_body = _last()["body"]
    assert turn_body["tools"][0]["function"]["name"] == "lookup"
    _assert_clean_body(_last())
    RESPONDERS["chat"] = lambda body: _chat_payload(
        body, content="done", total=7)
    turn_two = [
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": "", "tool_calls": [dict(tool_call)]},
        {"role": "tool", "tool_call_id": "call_1",
         "content": json.dumps({"result": "A"})},
    ]
    with llm_request_scope("run-tc", "owner-tc"):
        second = await client.complete_native(turn_two, [SPEC])
    assert second.protocol_error is None
    assert second.tool_calls == ()
    assert second.text == "done"
    assert second.total_tokens == 7
    sent = _last()["body"]["messages"]
    assert any(message.get("tool_call_id") == "call_1"
               and "A" in message.get("content", "") for message in sent)
    _assert_clean_body(_last())

async def test_responses_native_tool_call_round_trip(_sdk):
    call_item = {"type": "function_call", "id": "fc-1", "call_id": "call_9",
                 "name": "lookup", "arguments": json.dumps({"q": "b"})}
    RESPONDERS["responses"] = lambda body: _responses_payload(
        body, text="", total=6, output=[dict(call_item)])
    client = _build("openai/responses/gpt-tools")
    with llm_request_scope("run-tr", "owner-tr"):
        first = await client.complete_native([{"role": "user", "content": "go"}],
                                             [SPEC])
    assert first.protocol_error is None
    assert len(first.tool_calls) == 1
    assert first.tool_calls[0].call_id == "call_9"
    assert first.tool_calls[0].name == "lookup"
    assert first.tool_calls[0].arguments == {"q": "b"}
    assert first.total_tokens == 6
    turn_body = _last()["body"]
    assert turn_body["tools"][0]["name"] == "lookup"
    _assert_clean_body(_last())
    RESPONDERS["responses"] = lambda body: _responses_payload(
        body, text="done-b", total=7)
    turn_two = [
        {"role": "user", "content": "go"},
        dict(call_item),
        {"type": "function_call_output", "call_id": "call_9",
         "output": json.dumps({"result": "B"})},
    ]
    with llm_request_scope("run-tr", "owner-tr"):
        second = await client.complete_native(turn_two, [SPEC])
    assert second.protocol_error is None
    assert second.text == "done-b"
    sent = _last()["body"]["input"]
    assert any(item.get("type") == "function_call_output"
               and item.get("call_id") == "call_9"
               and "B" in item.get("output", "") for item in sent)
    _assert_clean_body(_last())

@pytest.mark.parametrize("model,path", [
    ("openai/gpt-worker-prefix", "chat"),
    ("openai/responses/gpt-worker-prefix", "responses"),
    ("anthropic/claude-worker-prefix", "messages"),
])
async def test_worker_frozen_prefix_survives_actual_sdk_wire(_sdk, model, path):
    from redteam.orchestration.worker_session import FrozenMessage

    def native_response(body):
        if path == "chat":
            return _chat_payload(body, content=None, tool_calls=[{
                "id": "call_original", "type": "function", "function": {
                    "name": "lookup", "arguments": '{"q":"fixture"}'},
            }])
        if path == "responses":
            return _responses_payload(body, output=[{
                "type": "function_call", "id": "fc_original", "call_id": "call_original",
                "name": "lookup", "arguments": '{"q":"fixture"}', "status": "completed",
            }])
        return {**_messages_payload(body), "stop_reason": "tool_use", "content": [{
            "type": "tool_use", "id": "call_original", "name": "lookup",
            "input": {"q": "fixture"},
        }]}

    RESPONDERS[path] = native_response
    client = _build(model)
    messages = [FrozenMessage.capture({"role": "user", "content": "One synthetic Task"},
                                     max_content_chars=1000).message()]
    with llm_request_scope("run-prefix", "worker-a"):
        response = await client.complete_native(messages, [SPEC], system="Stable Worker")
        call, = response.tool_calls
        messages.extend([FrozenMessage.capture(message, max_content_chars=2000).message()
                         for message in [{"role": "assistant", "content": None,
                             "tool_calls": [{"id": call.call_id, "type": "function",
                                 "function": {"name": call.name,
                                              "arguments": json.dumps(call.arguments)}}]},
                             {"role": "tool", "tool_call_id": call.call_id,
                              "content": json.dumps({"text": "frozen original", "offset": 4,
                                                     "limit": 20, "truncated": True})}]])
        await client.complete_native(messages, [SPEC], system="Stable Worker")
    first, second = [item["body"] for item in CAPTURED]
    field = "input" if path == "responses" else "messages"
    assert second[field][:len(first[field])] == first[field]
    assert second["tools"] == first["tools"]
    assert second.get("system") == first.get("system")
    assert second.get("instructions") == first.get("instructions")
    assert "frozen original" in json.dumps(second[field])
    assert "previous_response_id" not in second and "conversation" not in second


async def test_messages_native_tool_call_round_trip(_sdk):
    use_item = {"type": "tool_use", "id": "toolu_1", "name": "lookup",
                "input": {"q": "c"}}
    RESPONDERS["messages"] = lambda body: {
        "id": "msg-t1", "type": "message", "role": "assistant",
        "model": "test-model",
        "content": [{"type": "text", "text": "thinking"}, dict(use_item)],
        "stop_reason": "tool_use",
        "usage": {"input_tokens": 2, "output_tokens": 3},
    }
    client = _build("anthropic/claude-tools")
    with llm_request_scope("run-tm", "owner-tm"):
        first = await client.complete_native([{"role": "user", "content": "go"}],
                                             [SPEC])
    assert first.protocol_error is None
    assert len(first.tool_calls) == 1
    assert first.tool_calls[0].call_id == "toolu_1"
    assert first.tool_calls[0].name == "lookup"
    assert first.tool_calls[0].arguments == {"q": "c"}
    assert "lookup" in json.dumps(_last()["body"].get("tools", []))
    _assert_clean_body(_last())
    RESPONDERS["messages"] = lambda body: _messages_payload(
        body, text="done-m")
    turn_two = [
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": [{"type": "text", "text": "thinking"},
                                          dict(use_item)]},
        {"role": "tool", "tool_call_id": "toolu_1",
         "content": json.dumps({"result": "C"})},
    ]
    with llm_request_scope("run-tm", "owner-tm"):
        second = await client.complete_native(turn_two, [SPEC])
    assert second.protocol_error is None
    assert second.text == "done-m"
    sent_messages = _last()["body"]["messages"]
    blocks = [
        item
        for message in sent_messages
        for item in (message.get("content") or [])
        if isinstance(item, dict)
    ]
    results = [block for block in blocks if block.get("type") == "tool_result"]
    assert len(results) == 1, (
        "expected exactly one tool_result block: "
        + json.dumps(sent_messages, sort_keys=True)[:2000])
    assert results[0]["tool_use_id"] == "toolu_1"
    assert json.loads(results[0]["content"]) == {"result": "C"}
    _assert_clean_body(_last())

async def test_native_missing_call_identity_matches_port_contract(_sdk):
    chat = _build("openai/gpt-noid")
    RESPONDERS["chat"] = lambda body: _chat_payload(
        body, content="", total=6, tool_calls=[{"id": "", "type": "function",
        "function": {"name": "lookup", "arguments": "{}"}}])
    missing_id = await chat.complete_native([{"role": "user", "content": "go"}],
                                             [SPEC])
    assert missing_id.protocol_error == NATIVE_PROTOCOL_MISSING_CALL_ID
    assert missing_id.tool_calls == ()
    RESPONDERS["chat"] = lambda body: _chat_payload(
        body, content="", total=6, tool_calls=[{"id": "c2", "type": "function",
        "function": {"name": "", "arguments": "{}"}}])
    missing_name = await chat.complete_native(
        [{"role": "user", "content": "go"}], [SPEC])
    assert missing_name.protocol_error == NATIVE_PROTOCOL_MISSING_TOOL_NAME
    assert missing_name.tool_calls == ()
    resp = _build("openai/responses/gpt-noid")
    RESPONDERS["responses"] = lambda body: _responses_payload(
        body, text="", total=6, output=[{"type": "function_call", "id": "x",
        "call_id": "", "name": "lookup", "arguments": "{}"}])
    resp_no_id = await resp.complete_native([{"role": "user", "content": "go"}],
                                             [SPEC])
    assert resp_no_id.protocol_error == NATIVE_PROTOCOL_MISSING_CALL_ID
    RESPONDERS["responses"] = lambda body: _responses_payload(
        body, text="", total=6, output=[{"type": "function_call", "id": "x",
        "call_id": "c3", "name": "", "arguments": "{}"}])
    resp_no_name = await resp.complete_native(
        [{"role": "user", "content": "go"}], [SPEC])
    assert resp_no_name.protocol_error == NATIVE_PROTOCOL_MISSING_TOOL_NAME

async def test_rate_limited_single_attempt_without_secret_leak(_sdk):
    ERROR_PLAN["chat"] = {
        "status": 429,
        "payload": {"error": {"message": "synthetic rate limit",
                              "type": "rate_limit_error",
                              "code": "rate_limit_exceeded"}},
        "headers": {"retry-after": "2"},
    }
    client = _build("openai/gpt-429")
    with llm_request_scope("run-429", "owner-429"), pytest.raises(
            LLMProviderFailure) as exc_info:
        await client.complete(messages=[{"role": "user", "content": "hi"}])
    failure = exc_info.value
    assert failure.kind == "rate_limited"
    assert failure.retryable is True
    assert failure.http_status == 429
    # Retry-After is not asserted: the transport header is not surfaced on
    # the normalized failure for this SDK path, so callers back off on
    # kind/retryable alone. What matters here is one attempt and no secret.
    assert len(CAPTURED) == 1
    assert _SYNTH_KEY not in str(failure)
    assert _SYNTH_KEY not in json.dumps(failure.diagnostic_fields(),
                                         sort_keys=True)

async def test_auth_failure_is_not_retryable_and_hides_the_key(_sdk):
    ERROR_PLAN["chat"] = {
        "status": 401,
        "payload": {"error": {"message": "synthetic auth failure",
                              "type": "authentication_error",
                              "code": "invalid_api_key"}},
    }
    client = _build("openai/gpt-401")
    with llm_request_scope("run-401", "owner-401"), pytest.raises(
            LLMProviderFailure) as exc_info:
        await client.complete(messages=[{"role": "user", "content": "hi"}])
    failure = exc_info.value
    assert failure.kind == "auth"
    assert failure.retryable is False
    assert failure.http_status == 401
    assert len(CAPTURED) == 1
    assert _SYNTH_KEY not in str(failure)

async def test_responses_rate_limit_is_single_attempt(_sdk):
    ERROR_PLAN["responses"] = {
        "status": 429,
        "payload": {"error": {"message": "synthetic rate limit",
                              "type": "rate_limit_error",
                              "code": "rate_limit_exceeded"}},
        "headers": {"retry-after": "2"},
    }
    client = _build("openai/responses/gpt-429")
    with llm_request_scope("run-r429", "owner-r429"), pytest.raises(
            LLMProviderFailure) as exc_info:
        await client.complete(messages=[{"role": "user", "content": "hi"}])
    assert exc_info.value.kind == "rate_limited"
    assert exc_info.value.retryable is True
    assert len(CAPTURED) == 1
    assert _SYNTH_KEY not in str(exc_info.value)

async def test_messages_rate_limit_is_single_attempt(_sdk):
    ERROR_PLAN["messages"] = {
        "status": 429,
        "payload": {"type": "error",
                    "error": {"type": "rate_limit_error",
                              "message": "synthetic rate limit"}},
        "headers": {"retry-after": "3"},
    }
    client = _build("anthropic/claude-429")
    with llm_request_scope("run-m429", "owner-m429"), pytest.raises(
            LLMProviderFailure) as exc_info:
        await client.complete(messages=[{"role": "user", "content": "hi"}])
    assert exc_info.value.kind == "rate_limited"
    assert exc_info.value.retryable is True
    assert exc_info.value.http_status == 429
    # Retry-After intentionally not asserted (see the chat 429 test): one
    # attempt, correct classification, and no secret are the contract.
    assert len(CAPTURED) == 1
    assert _SYNTH_KEY not in str(exc_info.value)

async def test_cancellation_propagates_unwrapped_chat(_sdk, monkeypatch):
    async def _cancel(**kwargs):
        raise asyncio.CancelledError()
    monkeypatch.setattr(_sdk, "acompletion", _cancel)
    client = _build("openai/gpt-cancel")
    with llm_request_scope("run-cc", "owner-cc"), pytest.raises(
            asyncio.CancelledError):
        await client.complete(messages=[{"role": "user", "content": "hi"}])
    assert CAPTURED == []

async def test_cancellation_propagates_unwrapped_responses(_sdk, monkeypatch):
    async def _cancel(**kwargs):
        raise asyncio.CancelledError()
    monkeypatch.setattr(_sdk, "aresponses", _cancel)
    client = _build("openai/responses/gpt-cancel")
    with llm_request_scope("run-cr", "owner-cr"), pytest.raises(
            asyncio.CancelledError):
        await client.complete(messages=[{"role": "user", "content": "hi"}])
    assert CAPTURED == []

async def test_cancellation_propagates_unwrapped_messages(_sdk, monkeypatch):
    async def _cancel(**kwargs):
        raise asyncio.CancelledError()
    monkeypatch.setattr(_sdk, "acompletion", _cancel)
    client = _build("anthropic/claude-cancel")
    with llm_request_scope("run-cm", "owner-cm"), pytest.raises(
            asyncio.CancelledError):
        await client.complete(messages=[{"role": "user", "content": "hi"}])
    assert CAPTURED == []

async def test_outgoing_calls_disable_hidden_sdk_retries(_sdk, monkeypatch):
    seen = []
    real_acompletion = _sdk.acompletion
    real_aresponses = _sdk.aresponses
    async def _spy_ac(**kwargs):
        seen.append(("acompletion", kwargs.get("num_retries")))
        return await real_acompletion(**kwargs)
    async def _spy_ar(**kwargs):
        seen.append(("aresponses", kwargs.get("num_retries")))
        return await real_aresponses(**kwargs)
    monkeypatch.setattr(_sdk, "acompletion", _spy_ac)
    monkeypatch.setattr(_sdk, "aresponses", _spy_ar)
    with llm_request_scope("run-nr", "owner-nr"):
        await _build("openai/gpt-nr").complete(
            messages=[{"role": "user", "content": "hi"}])
        await _build("openai/responses/gpt-nr").complete(
            messages=[{"role": "user", "content": "hi"}])
        await _build("anthropic/claude-nr").complete(
            messages=[{"role": "user", "content": "hi"}])
    assert seen == [("acompletion", 0), ("aresponses", 0), ("acompletion", 0)]

@pytest.mark.parametrize("key", ["extra_headers", "headers", "api_base",
                                 "api_key", "custom_llm_provider", "client",
                                 "http_client", "base_url", "auth_token",
                                 "use_bearer_for_custom_base",
                                 "use_responses_api"])
async def test_per_call_retarget_keys_rejected(_sdk, key):
    client = _build("openai/gpt-guard")
    with pytest.raises(ValueError, match="per-call override rejected"):
        await client.complete(messages=[{"role": "user", "content": "hi"}],
                              **{key: "x"})
    assert CAPTURED == []

async def test_guards_cover_structured_and_native_entries(_sdk):
    client = _build("openai/gpt-guard2")
    with pytest.raises(ValueError, match="per-call override rejected"):
        await client.complete_structured(
            messages=[{"role": "user", "content": "hi"}],
            response_schema=SCHEMA, api_base="https://example.invalid/")
    with pytest.raises(ValueError, match="per-call override rejected"):
        await client.complete_native([{"role": "user", "content": "hi"}],
                                     [SPEC], client="x")
    assert CAPTURED == []

@pytest.mark.parametrize("bad", [1, 2, True])
async def test_nonzero_num_retries_rejected(_sdk, bad):
    client = _build("openai/gpt-nr2")
    with pytest.raises(ValueError, match="num_retries"):
        await client.complete(messages=[{"role": "user", "content": "hi"}],
                              num_retries=bad)
    assert CAPTURED == []

async def test_zero_and_false_num_retries_pass_through(_sdk):
    client = _build("openai/gpt-nr3")
    with llm_request_scope("run-nz", "owner-nz"):
        assert await client.complete(
            messages=[{"role": "user", "content": "hi"}],
            num_retries=0) == "hello-chat"
        assert await client.complete(
            messages=[{"role": "user", "content": "hi"}],
            num_retries=False) == "hello-chat"

def test_build_rejects_non_go_provider(_sdk):
    with pytest.raises(ValueError, match="requires provider opencode-go"):
        go.build(LLMConfig(provider="litellm", default_model="openai/gpt-x",
                           api_key_env_var=_KEY_ENV))

@pytest.mark.parametrize("model", ["gpt-4o-mini", "openai/responses/",
                                   "openai/   ", "anthropic/"])
def test_build_rejects_unqualified_models(_sdk, model):
    with pytest.raises(ValueError, match="REDTEAM_LLM_MODEL"):
        go.build(_config(model))

def test_build_rejects_missing_key(_sdk, monkeypatch):
    monkeypatch.delenv(_KEY_ENV, raising=True)
    with pytest.raises(ValueError, match="requires an API key"):
        go.build(_config("openai/gpt-x"))

def test_build_canonicalizes_padded_model(_sdk):
    assert go.build(_config("  openai/gpt-pad  ")).model == "openai/gpt-pad"


@pytest.mark.parametrize("model,mode", [("cl-x", "enabled"), ("claude-opus-4-6", "adaptive")])
async def test_effort_messages_omits_thinking_and_replays_signature(_sdk, model, mode):
    signature = "encrypted-signature-fixture"
    def first(body):
        payload = _messages_payload(body)
        payload["content"] = [
            {"type": "thinking", "thinking": "", "signature": signature},
            {"type": "tool_use", "id": "call-one", "name": "inspect", "input": {}},
        ]
        payload["stop_reason"] = "tool_use"
        return payload
    RESPONDERS["messages"] = first
    client = go.build(LLMConfig(
        provider="opencode-go", default_model="anthropic/" + model,
        api_key_env_var=_KEY_ENV, reasoning_effort="high",
    ))
    tools = [NativeToolSpec("inspect", "Read fixture", {"type": "object"})]
    result = await client.complete_native(messages=[{"role": "user", "content": "inspect"}],
                                          tools=tools)
    body = CAPTURED[-1]["body"]
    assert body["thinking"]["type"] == mode
    assert body["thinking"]["display"] == "omitted"
    if mode == "enabled":
        assert body["thinking"]["budget_tokens"] == 4096  # SDK's high mapping
    assert body["output_config"]["effort"] == "high"
    assert result.continuation == ({"type": "thinking", "thinking": "", "signature": signature},)
    RESPONDERS["messages"] = lambda body: _messages_payload(body)
    await client.complete_native(messages=[
        {"role": "user", "content": "inspect"},
        {"role": "assistant", "content": "", "provider_continuation": list(result.continuation),
         "tool_calls": [{"id": "call-one", "type": "function",
                         "function": {"name": "inspect", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "call-one", "content": "fixture result"},
    ], tools=tools)
    assert CAPTURED[-1]["body"]["messages"][1]["content"][0] == result.continuation[0]


async def test_effort_responses_requests_and_replays_encrypted_items(_sdk):
    encrypted = {"type": "reasoning", "id": "reason-one",
                 "encrypted_content": "ciphertext-fixture", "summary": []}
    RESPONDERS["responses"] = lambda body: _responses_payload(body, output=[
        encrypted,
        {"type": "function_call", "id": "item-one", "call_id": "call-one",
         "name": "inspect", "arguments": "{}", "status": "completed"},
    ])
    client = go.build(LLMConfig(
        provider="opencode-go", default_model="openai/responses/gpt-x",
        api_key_env_var=_KEY_ENV, reasoning_effort="high",
    ))
    tools = [NativeToolSpec("inspect", "Read fixture", {"type": "object"})]
    result = await client.complete_native(messages=[{"role": "user", "content": "inspect"}],
                                          tools=tools)
    assert "reasoning.encrypted_content" in CAPTURED[-1]["body"]["include"]
    assert CAPTURED[-1]["body"]["reasoning"]["effort"] == "high"
    assert result.continuation == (encrypted,)
    RESPONDERS["responses"] = lambda body: _responses_payload(body)
    await client.complete_native(messages=[
        {"role": "assistant", "provider_continuation": list(result.continuation),
         "tool_calls": [{"id": "call-one", "type": "function",
                         "function": {"name": "inspect", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "call-one", "content": "fixture result"},
    ], tools=tools)
    assert CAPTURED[-1]["body"]["input"][0] == encrypted


async def test_effort_chat_reaches_http_body(_sdk):
    client = go.build(LLMConfig(
        provider="opencode-go", default_model="openai/gpt-x",
        api_key_env_var=_KEY_ENV, reasoning_effort="high",
    ))
    assert await client.complete(messages=[{"role": "user", "content": "fixture"}]) == "hello-chat"
    assert CAPTURED[-1]["body"]["reasoning_effort"] == "high"
