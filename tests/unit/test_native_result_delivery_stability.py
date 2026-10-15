"""Stable bounded delivery of native tool results (native_result_delivery owner)."""

from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from types import SimpleNamespace

from redteam.core.contracts.evidence import EvidenceRecord
from redteam.core.enums import EvidenceSourceType
from redteam.orchestration.checkpointing import build_checkpointer
from redteam.orchestration.evidence_access import EvidenceReader
from redteam.orchestration.native_result_delivery import (
    ORIGINAL_PREVIEW_CHARS,
    READ_DELIVERY_MAX_CHARS,
    deliver_tool_messages,
)
from redteam.orchestration.nodes.react import _result_messages
from tests.fakes.evidence_store import FakeEvidenceStore

SECRET = "fixture-private-value"


def _record(index, *, run_id="run", observed=None, metadata=None, source=EvidenceSourceType.TOOL):
    body = observed if observed is not None else (
        f"marker-{index} " + "lorem ipsum dolor sit amet " * 500
    )
    return EvidenceRecord.model_validate({
        "evidence_id": f"ev-{index}", "run_id": run_id, "engagement_id": "eng",
        "action_id": f"action-{index}", "invocation_id": f"inv-{index}",
        "source_type": source, "source_id": f"provider-{index}",
        "content_type": "application/json", "digest": "a" * 64,
        "created_at": datetime.now(timezone.utc), "summary": "Response observed",
        "metadata": metadata if metadata is not None else {"tool_result": {
            "success": True, "output": {"observed": body, "api_key": SECRET},
        }},
    })


async def _store(count):
    store = FakeEvidenceStore()
    for index in range(count):
        await store.append(_record(index))
    return store


def _round(call_id, body):
    return [
        {"role": "user", "content": "Current state"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": call_id, "type": "function",
         "function": {"name": "tool", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": call_id, "content": json.dumps(body)},
    ]


def _executed(index):
    return _round(f"call-{index}", {"ok": True, "executed": True,
                                     "evidence_ids": [f"ev-{index}"]})


def _read(call_id, index, *, offset=0, limit=4000, query=None):
    return _round(call_id, {"ok": True, "payload": {"status": "ok"},
                            "evidence_read_request": {"evidence_id": f"ev-{index}",
                            "offset": offset, "limit": limit, "query": query}})


def _tool_contents(delivered):
    return [message["content"] for message in delivered if message["role"] == "tool"]


RUNTIME = SimpleNamespace(run_id="run", engagement_id="eng")


async def _deliver(store, messages):
    return await deliver_tool_messages(messages, evidence_store=store, runtime=RUNTIME)


async def test_preview_is_fixed_and_identical_as_later_results_accumulate():
    store = await _store(12)
    history = _executed(0)
    first = _tool_contents(await _deliver(store, history))
    grown = history + _executed(1) + _executed(2) + _executed(3) + _executed(4)
    later = _tool_contents(await _deliver(store, grown))
    assert later[0] == first[0]
    assert json.loads(first[0])["original_results"][0]["truncated"] is True
    assert len(json.loads(first[0])["original_results"][0]["text"]) <= ORIGINAL_PREVIEW_CHARS
    assert json.loads(later[-1])["original_results"][0]["offset"] == 0


async def test_explicit_read_keeps_offset_query_and_reports_the_clamp():
    store = await _store(1)
    messages = _read("call-read", 0, offset=10, limit=20000, query="marker")
    delivered = await _deliver(store, messages)
    body = json.loads(_tool_contents(delivered)[0])
    assert body["delivery"] == {
        "requested_limit": 20000, "delivered_limit": READ_DELIVERY_MAX_CHARS,
    }
    expected = await EvidenceReader(store).read(
        "ev-0", "run", engagement_id="eng", offset=10,
        limit=READ_DELIVERY_MAX_CHARS, query="marker",
    )
    assert body["payload"]["text"] == expected["text"]
    assert body["payload"]["query"] == "marker" and body["payload"]["offset"] == 10
    assert body["payload"]["evidence_id"] == "ev-0"


async def test_read_window_is_not_resized_by_later_results():
    store = await _store(8)
    messages = _read("call-read", 0, limit=2000)
    first = _tool_contents(await _deliver(store, messages))[0]
    grown = messages + _executed(1) + _executed(2) + _executed(3) + _read("call-b", 4)
    later = _tool_contents(await _deliver(store, grown))[0]
    assert later == first
    assert json.loads(first)["delivery"]["delivered_limit"] == 2000


async def test_missing_other_run_and_no_raw_stay_explicit_without_text():
    store = FakeEvidenceStore()
    await store.append(_record(0, run_id="other-run"))
    await store.append(_record(1, metadata={"receipt_kind": "plain"}))
    messages = (_read("c1", 9) + _read("c2", 0) + _read("c3", 1))
    delivered = _tool_contents(await _deliver(store, messages))
    statuses = [json.loads(item)["payload"]["status"] for item in delivered]
    assert statuses == ["not_found", "run_mismatch", "no_raw"]
    for item in delivered:
        payload = json.loads(item)["payload"]
        assert payload.get("text", "") == ""


async def test_input_history_and_protocol_fields_are_not_mutated():
    store = await _store(3)
    history = [*_executed(0), *_read("call-read", 1), *_executed(2)]
    history[1]["provider_continuation"] = [{"type": "thinking", "signature": "sig-abc"}]
    original = copy.deepcopy(history)
    delivered = await _deliver(store, history)
    assert history == original
    non_tool = [m for m in original if m["role"] != "tool"]
    assert [m for m in delivered if m["role"] != "tool"] == non_tool
    tool_ids = [m["tool_call_id"] for m in delivered if m["role"] == "tool"]
    assert tool_ids == ["call-0", "call-read", "call-2"]
    assert delivered[1]["provider_continuation"] == original[1]["provider_continuation"]
    assert SECRET not in json.dumps(delivered)


async def test_checkpoint_round_trip_delivers_the_same_bytes():
    store = await _store(4)
    history = [*_executed(0), *_read("call-read", 1, limit=800), *_executed(2)]
    saver = build_checkpointer(enabled=True)
    restored = saver.serde.loads_typed(saver.serde.dumps_typed({"native_messages": history}))
    assert _tool_contents(await _deliver(store, restored["native_messages"])) == \
        _tool_contents(await _deliver(store, history))


async def test_react_delegate_matches_the_owner():
    store = await _store(2)
    history = [*_executed(0), *_read("call-read", 1)]
    deps = SimpleNamespace(evidence_store=store)
    assert await _result_messages(history, deps=deps, runtime=RUNTIME) == \
        await _deliver(store, history)
