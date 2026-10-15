"""Pure session/checkpoint contracts; live consumer tests are integration tests."""

import json

import pytest
from langgraph.checkpoint.base import empty_checkpoint
from langgraph.checkpoint.sqlite import SqliteSaver

from redteam.orchestration.state import RuntimeState
from redteam.orchestration.worker_session import FrozenMessage, SessionBinding, WorkerSession
from tests.unit.test_central_state import ENG, MISSION, RUN, task


def binding(*, task_id="task-a", instance="instance-a", **changes):
    runtime = RuntimeState(
        run_id=RUN, engagement_id=ENG, mission_id=MISSION,
        active_task_id=task_id, instance_id=instance,
    )
    return SessionBinding.capture(
        runtime.model_copy(update=changes), task(task_id), system="Stable system",
        tools=[], provider_binding={"config_fingerprint": "selected-provider"},
    )


def test_session_identity_is_separate_from_shared_prefix():
    first = binding()
    second = binding(task_id="task-b", instance="instance-b")
    assert first.identity != second.identity
    assert first.prefix_identity == second.prefix_identity
    assert binding(task_ownership_version=2) == first  # recovery epoch is not a new conversation
    checkpoint = WorkerSession(binding=first).checkpoint()
    assert WorkerSession.restore(checkpoint, expected=first).binding == first
    for wrong in (second, binding(instance="instance-b"), first.model_copy(update={
        "purpose_hash": "a" * 64,
    }), first.model_copy(update={"tools_hash": "a" * 64})):
        with pytest.raises(ValueError, match="binding mismatch"):
            WorkerSession.restore(checkpoint, expected=wrong)


def test_capture_rejects_unbound_task_and_changed_candidate_revision():
    with pytest.raises(ValueError, match="matching admitted task"):
        binding(active_task_id="different")
    with pytest.raises(ValueError, match="candidate revisions mismatch"):
        binding(probe_hypotheses={"h1": {"revision": 2}})


def test_display_checkpoint_is_immutable_masked_bounded_and_replay_exact(tmp_path):
    # Fixed synthetic content only, including a selected window's provenance.
    window = {"text": "fixture-output", "offset": 12, "limit": 40, "truncated": True,
              "provenance": {"evidence_id": "ev-fixture"}, "password": "fixture-secret"}
    message = {"role": "tool", "tool_call_id": "call-original",
               "content": json.dumps(window)}
    frozen = FrozenMessage.capture(message, max_content_chars=1000)
    original = frozen.message()
    assert "fixture-secret" not in frozen.display_json
    assert json.loads(original["content"])["offset"] == 12
    assert json.loads(original["content"])["limit"] == 40
    message["content"] = "changed"
    frozen.message()["content"] = "also changed"
    assert frozen.message() == original
    assert "fixture-output" not in repr(frozen)
    assistant = FrozenMessage.capture({"role": "assistant", "content": "Public result",
        "provider_continuation": [{"type": "thinking", "thinking": "",
                                    "signature": "opaque-fixture-signature"}]},
        max_content_chars=1000)
    session = WorkerSession(binding=binding()).append(frozen, assistant)
    checkpoint = empty_checkpoint()
    checkpoint["channel_values"] = {"worker_session": session.checkpoint()}
    database = str(tmp_path / "checkpoint.sqlite")
    with SqliteSaver.from_conn_string(database) as saver:
        config = saver.put({"configurable": {"thread_id": f"{RUN}:instance:instance-a",
                                             "checkpoint_ns": ""}}, checkpoint, {}, {})
    with SqliteSaver.from_conn_string(database) as saver:
        recovered = saver.get_tuple(config)
    assert recovered is not None
    restored = WorkerSession.restore(
        recovered.checkpoint["channel_values"]["worker_session"], expected=binding(),
    )
    assert restored == session
    assert restored.messages[0].display_json == frozen.display_json
    assert restored.messages[1].message()["provider_continuation"] == (
        assistant.message()["provider_continuation"]
    )
    with pytest.raises(ValueError, match="bounded public display"):
        FrozenMessage.capture({"role": "tool", "content": "long"}, max_content_chars=3)
    with pytest.raises(ValueError, match="invalid public transcript"):
        FrozenMessage.capture({"role": "assistant", "reasoning_content": "private"},
                              max_content_chars=1000)


def test_waiting_is_not_closed_and_cannot_append_until_explicitly_resumed():
    waiting = WorkerSession(binding=binding(), status="waiting", wait_reason="approval_pending")
    closed = WorkerSession(binding=binding(), status="closed")
    assert waiting.status != closed.status
    for session in (waiting, closed):
        assert WorkerSession.restore(session.checkpoint(), expected=binding()) == session
        with pytest.raises(ValueError, match="not open"):
            session.append()
    with pytest.raises(ValueError, match="wait reason"):
        WorkerSession(binding=binding(), status="waiting")


def test_public_arguments_are_masked_and_the_entire_display_is_bounded():
    original = {"role": "assistant", "content": None, "tool_calls": [{
        "id": "call-a", "type": "function", "function": {
            "name": "fixture", "arguments": '{"password":"fixture-secret","count":1}'},
    }]}
    frozen = FrozenMessage.capture(original, max_content_chars=1000)
    assert "fixture-secret" not in frozen.display_json
    assert "fixture-secret" in original["tool_calls"][0]["function"]["arguments"]
    assert json.loads(frozen.message()["tool_calls"][0]["function"]["arguments"]) == {"count": 1}
    with pytest.raises(ValueError, match="bounded public display"):
        FrozenMessage.capture(original, max_content_chars=50)
