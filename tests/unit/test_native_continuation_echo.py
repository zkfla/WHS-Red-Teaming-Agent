from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import redteam.core.ports.llm as llm_module
from redteam.core.contracts.mission import redact_secret_material
from redteam.core.ports.llm import NativeCompletion, NativeToolCall
from redteam.orchestration.nodes.react import (
    _echo_provider_continuation,
    _result_messages,
)
from redteam.orchestration.worker_session import FrozenMessage

CIPHER = "enc-abc-123-signature-ciphertext"


def _thinking_block():
    return {"type": "thinking", "thinking": "", "signature": CIPHER}


def test_empty_default_keeps_old_compat():
    first = NativeCompletion(tool_calls=(), text="hi")
    assert first.continuation == ()
    again = NativeCompletion(tool_calls=(), text="hi", continuation=())
    assert again.continuation == ()
    assert first == again


def test_continuation_round_trips_opaque_blocks():
    block = _thinking_block()
    completion = NativeCompletion(tool_calls=(), text="ok", continuation=[block])
    assert isinstance(completion.continuation, tuple)
    assert completion.continuation[0]["signature"] == CIPHER
    assert completion.continuation[0]["thinking"] == ""
    assert block == _thinking_block()


def test_continuation_absent_from_repr():
    completion = NativeCompletion(tool_calls=(), text="ok", continuation=[_thinking_block()])
    assert CIPHER not in repr(completion)
    assert "signature" not in repr(completion)


def test_malformed_continuation_rejected_without_invention():
    with pytest.raises(ValueError):
        NativeCompletion(tool_calls=(), text="x", continuation=("not-a-dict",))
    with pytest.raises(ValueError):
        NativeCompletion(tool_calls=(), text="x", continuation=[{123: "bad-key"}])
    with pytest.raises(ValueError):
        NativeCompletion(tool_calls=(), text="x", continuation="ciphertext")
    with pytest.raises(ValueError):
        NativeCompletion(tool_calls=(), text="x", continuation=[{"v": object()}])
    with pytest.raises(ValueError):
        NativeCompletion(tool_calls=(), text="x", continuation=[{"a": {"b": {1: "x"}}}])
    with pytest.raises(ValueError):
        NativeCompletion(tool_calls=(), text="x", continuation=[{"v": float("nan")}])
    over = [{"i": i} for i in range(17)]
    with pytest.raises(ValueError):
        NativeCompletion(tool_calls=(), text="x", continuation=over)


def test_continuation_bound_measures_bytes_not_chars(monkeypatch):
    monkeypatch.setattr(llm_module, "NATIVE_MAX_CONTINUATION_BYTES", 100)
    ascii_block = {"t": "a" * 80}
    NativeCompletion(tool_calls=(), text="x", continuation=[ascii_block])
    wide_block = {"t": "e" * 80}
    wide_block = {"t": "\u00e9" * 80}
    with pytest.raises(ValueError):
        NativeCompletion(tool_calls=(), text="x", continuation=[wide_block])


def test_echo_sets_field_only_when_nonempty():
    assert _echo_provider_continuation(SimpleNamespace(continuation=())) is None
    assert _echo_provider_continuation(SimpleNamespace(continuation=[])) is None
    assert _echo_provider_continuation(object()) is None
    echoed = _echo_provider_continuation(SimpleNamespace(continuation=(_thinking_block(),)))
    assert echoed == [_thinking_block()]


def test_echo_rejects_malformed_loudly_without_mutation():
    raw = [{"type": "thinking", "signature": CIPHER}]
    snapshot = json.loads(json.dumps(raw))
    with pytest.raises(ValueError, match="native provider continuation"):
        _echo_provider_continuation(SimpleNamespace(continuation=["nope"]))
    with pytest.raises(ValueError, match="native provider continuation"):
        _echo_provider_continuation(SimpleNamespace(continuation="cipher"))
    with pytest.raises(ValueError, match="native provider continuation"):
        _echo_provider_continuation(SimpleNamespace(continuation=[{123: 1}]))
    assert raw == snapshot


def test_echo_returns_deepcopy():
    response = SimpleNamespace(continuation=(_thinking_block(),))
    echoed = _echo_provider_continuation(response)
    assert echoed == [_thinking_block()]
    echoed[0]["signature"] = "mutated"
    assert response.continuation[0]["signature"] == CIPHER


def test_checkpoint_reopen_replays_exact_ciphertext(tmp_path):
    from langgraph.checkpoint.base import empty_checkpoint
    from langgraph.checkpoint.sqlite import SqliteSaver

    assistant = {
        "role": "assistant",
        "content": "working",
        "provider_continuation": [_thinking_block()],
    }
    user = {"role": "user", "content": "snapshot-1"}
    tool = {"role": "tool", "tool_call_id": "c1", "content": json.dumps({"ok": True})}
    stored = [user, assistant, tool]
    checkpoint = empty_checkpoint()
    checkpoint["channel_values"] = {"native_messages": stored}
    config = {"configurable": {"thread_id": "effort-fixture", "checkpoint_ns": ""}}
    database = str(tmp_path / "checkpoint.sqlite")
    with SqliteSaver.from_conn_string(database) as saver:
        saved_config = saver.put(config, checkpoint, {}, {})
    with SqliteSaver.from_conn_string(database) as saver:
        recovered = saver.get_tuple(saved_config)
    assert recovered is not None
    messages = recovered.checkpoint["channel_values"]["native_messages"]
    reopened = [FrozenMessage.capture(message, max_content_chars=10000).message()
                for message in messages]
    found = [m for m in reopened if m.get("role") == "assistant"][0]
    assert found["provider_continuation"] == [_thinking_block()]
    assert found["provider_continuation"][0]["signature"] == CIPHER
    assert found["content"] == "working"
    assert "signature" not in json.dumps({"content": found["content"]})


def test_frozen_displays_preserve_user_text_and_continuation_untouched():
    assistant = {"role": "assistant", "content": "t", "provider_continuation": [_thinking_block()]}
    messages = [{"role": "user", "content": "old"}, assistant]
    frozen = [FrozenMessage.capture(message, max_content_chars=10000) for message in messages]
    assert frozen[0].message()["content"] == "old"
    appended = [*frozen, FrozenMessage.capture({"role": "user", "content": "new"},
                                              max_content_chars=10000)]
    assert appended[:len(frozen)] == frozen
    kept = [m.message() for m in appended if m.message().get("role") == "assistant"]
    assert kept and kept[0]["provider_continuation"] == [_thinking_block()]


async def test_result_messages_keep_assistant_reference_only():
    assistant = {"role": "assistant", "content": "t", "provider_continuation": [_thinking_block()]}
    user = {"role": "user", "content": "u"}
    deps = SimpleNamespace(evidence_store=SimpleNamespace())
    runtime = SimpleNamespace(run_id="run-1")
    delivered = await _result_messages([user, assistant], deps=deps, runtime=runtime)
    kept = [m for m in delivered if m.get("role") == "assistant"][0]
    assert kept["provider_continuation"] == [_thinking_block()]
    assert kept["provider_continuation"][0]["signature"] == CIPHER


def test_redactor_leaves_signature_untouched():
    assistant = {
        "role": "assistant",
        "content": "hello",
        "provider_continuation": [_thinking_block()],
    }
    redacted = redact_secret_material(assistant)
    assert redacted["provider_continuation"] == [_thinking_block()]
    assert redacted["provider_continuation"][0]["signature"] == CIPHER
    assert assistant["provider_continuation"] == [_thinking_block()]


def test_signed_sdk_call_id_echoed_exactly_unchanged():
    # Gemini/Vertex thought signatures travel inside the SDK tool-call id, so the
    # id itself is the continuation carrier and must pass through byte-exact.
    signed_id = "call_abc123__sig_" + "Q" * 200
    call = NativeToolCall(call_id=signed_id, name="lookup", arguments={"q": "x"})
    completion = NativeCompletion(tool_calls=(call,), text="ok")
    assert completion.tool_calls[0].call_id == signed_id
    # react checkpoints requested.call_id verbatim as the assistant tool_calls id,
    # with no re-encoding or truncation; the JSON checkpoint round-trip below is
    # that same persisted path.
    checkpointed = json.loads(json.dumps([{"role": "assistant", "tool_calls":
        [{"id": completion.tool_calls[0].call_id}]}]))
    assert checkpointed[0]["tool_calls"][0]["id"] == signed_id
    declaration = FrozenMessage.capture({"role": "assistant", "content": None, "tool_calls": [{
        "id": signed_id, "type": "function", "function": {"name": "lookup", "arguments": "{}"},
    }]}, max_content_chars=10000)
    answer = FrozenMessage.capture({"role": "tool", "tool_call_id": signed_id,
                                   "content": "Public result"}, max_content_chars=10000)
    assert declaration.message()["tool_calls"][0]["id"] == signed_id
    assert answer.message()["tool_call_id"] == signed_id
    # No Google plaintext reasoning is retained alongside the signed id.
    assert not hasattr(completion, "thought")
    assert not hasattr(completion, "reasoning")
    assert "thought" not in completion.tool_calls[0].__dict__
