"""Historical result elision in native delivery (native_result_delivery owner)."""

from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from types import SimpleNamespace

from redteam.core.contracts.evidence import EvidenceRecord
from redteam.core.contracts.mission import redact_secret_material
from redteam.core.enums import EvidenceSourceType
from redteam.orchestration import native_result_delivery as delivery
from redteam.orchestration.checkpointing import build_checkpointer
from redteam.orchestration.evidence_access import EvidenceReader
from redteam.orchestration.native_result_delivery import (
    ELIDED_NOTE,
    READ_DELIVERY_MAX_CHARS,
    deliver_tool_messages,
)
from tests.fakes.evidence_store import FakeEvidenceStore

SECRET = "fixture-private-value"
BODY = "lorem ipsum dolor sit amet " * 400
RUNTIME = SimpleNamespace(run_id="run", engagement_id="eng")


class CountingStore(FakeEvidenceStore):
    def __init__(self) -> None:
        super().__init__()
        self.gets = 0

    async def get(self, evidence_id):
        self.gets += 1
        return await super().get(evidence_id)


def _original(index, *, ok=True, run_id="run"):
    raw = {
        "success": ok,
        "tool_id": "demo.tool",
        "output": {"observed": f"marker-{index} " + BODY, "api_key": SECRET},
    }
    if not ok:
        raw["error"] = {"code": "tool_failed", "message": "fail-marker"}
    return EvidenceRecord.model_validate(
        {
            "evidence_id": f"ev-{index}",
            "run_id": run_id,
            "engagement_id": "eng",
            "action_id": f"action-{index}",
            "invocation_id": f"inv-{index}",
            "source_type": EvidenceSourceType.TOOL,
            "source_id": f"provider-{index}",
            "content_type": "application/json",
            "digest": "a" * 64,
            "created_at": datetime.now(timezone.utc),
            "summary": "Response observed",
            "metadata": {"tool_result": raw},
        }
    )


def _local(local_id, result, *, run_id="run"):
    return EvidenceRecord.model_validate(
        {
            "evidence_id": local_id,
            "run_id": run_id,
            "engagement_id": "eng",
            "source_type": EvidenceSourceType.SYSTEM,
            "source_id": "orchestration.runtime_tool",
            "content_type": "application/json",
            "digest": "b" * 64,
            "created_at": datetime.now(timezone.utc),
            "summary": "Local tool result",
            "metadata": {
                "record_kind": "runtime_tool_result",
                "tool_id": "evidence.read",
                "model_tool_call_id": local_id,
                "mission_id": None,
                "mission_task_id": None,
                "result": result,
            },
        }
    )


async def _store(*, locals_=()):
    store = CountingStore()
    for index in range(5):
        await store.append(_original(index))
    await store.append(_original(9, run_id="other-run"))
    for record in locals_:
        await store.append(record)
    return store


async def _stored_read(store, evidence_id, local_id, *, offset=0, limit=4000, query=None):
    """Stored explicit-read body, as _local_call keeps it: window text omitted."""
    window = await EvidenceReader(store).read(
        evidence_id=evidence_id,
        run_id="run",
        engagement_id="eng",
        offset=offset,
        limit=limit,
        query=query,
    )
    payload = {key: value for key, value in window.items() if key not in {"text", "program_output"}}
    payload["text_omitted"] = True
    return {
        "ok": True,
        "payload": payload,
        "refs": [evidence_id],
        "evidence_ids": [local_id],
        "evidence_read_request": {
            "evidence_id": evidence_id,
            "offset": offset,
            "limit": limit,
            "query": query,
        },
    }


def _round(call_id, body):
    return [
        {"role": "user", "content": "Current state"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": call_id, "type": "function", "function": {"name": "tool", "arguments": "{}"}}
            ],
        },
        {"role": "tool", "tool_call_id": call_id, "content": json.dumps(body)},
    ]


def _executed(call_id, evidence_id):
    return _round(call_id, {"ok": True, "executed": True, "evidence_ids": [evidence_id]})


def _tool_bodies(messages):
    return [json.loads(item["content"]) for item in messages if item.get("role") == "tool"]


def _size(messages):
    return len(json.dumps(messages, ensure_ascii=False).encode("utf-8"))


async def _deliver(store, messages):
    return await deliver_tool_messages(messages, evidence_store=store, runtime=RUNTIME)


async def test_historical_explicit_read_is_elided_and_its_reread_pointer_resolves():
    store = await _store(locals_=[_local("loc-r0", {"ok": True})])
    body = await _stored_read(store, "ev-0", "loc-r0")
    messages = [*_round("c0", body), *_executed("c1", "ev-1"), *_executed("c2", "ev-2")]
    delivered = await _deliver(store, messages)
    old = _tool_bodies(delivered)[0]
    assert old["elided"] == ELIDED_NOTE
    assert old["reread"] == {"evidence_id": "ev-0", "offset": 0, "limit": 4000, "query": None}
    assert "evidence_read_request" not in old and '"text"' not in json.dumps(old)
    assert old["evidence_ids"] == ["loc-r0"]
    pointer = old["reread"]
    reread = await EvidenceReader(store).read(
        pointer["evidence_id"],
        "run",
        engagement_id="eng",
        offset=pointer["offset"],
        limit=pointer["limit"],
        query=pointer["query"],
    )
    assert reread["status"] == "ok" and reread["text"]


async def test_historical_executed_original_keeps_its_preview_window():
    store = await _store()
    messages = [*_executed("c0", "ev-3"), *_executed("c1", "ev-1"), *_executed("c2", "ev-2")]
    delivered = _tool_bodies(await _deliver(store, messages))
    assert "reread" not in delivered[0] and "elided" not in delivered[0]
    assert delivered[0]["original_results"][0]["evidence_id"] == "ev-3"
    assert delivered[0]["original_results"][0]["offset"] == 0


async def test_error_and_execution_status_survive_elision_with_redaction():
    store = await _store()
    await store.append(_original(7, ok=False))
    await store.append(_local("loc-f0", {"ok": True}))
    body = await _stored_read(store, "ev-7", "loc-f0")
    messages = [*_round("c0", body), *_executed("c1", "ev-2"), *_executed("c2", "ev-3")]
    delivered = await _deliver(store, messages)
    old = _tool_bodies(delivered)[0]
    assert old["payload"]["execution"]["error"]["message"] == "fail-marker"
    assert old["payload"]["status"] == "ok" and old["payload"]["truncated"] is True
    assert SECRET not in json.dumps(delivered)


async def test_long_error_text_is_kept_whole_and_readback_resolves_bulky_payload():
    long_error = "E" * 900
    long_reason = "R" * 700
    records = [
        {"kind": "record", "identity": f"n-{index}", "text": "x" * 200} for index in range(40)
    ]
    result = {
        "ok": False,
        "error": long_error,
        "payload": {
            "status": "ok",
            "reason": long_reason,
            "records": records,
            "context": {"needle": "needle-XYZ", "nested": {"depth": {"items": list(range(300))}}},
        },
    }
    store = await _store(locals_=[_local("loc-big", result)])
    body = {**result, "evidence_ids": ["loc-big"]}
    messages = [*_round("c0", body), *_executed("c1", "ev-2"), *_executed("c2", "ev-3")]
    delivered = await _deliver(store, messages)
    old = _tool_bodies(delivered)[0]
    assert old["error"] == long_error
    assert old["payload"]["reason"] == long_reason
    assert old["payload"]["records"]["elided_chars"] > 0 and "records" in old["payload"]
    assert "context" in old["payload"] and old["payload"]["context"]["elided_chars"] > 0
    pointer = old["reread"]
    assert pointer["evidence_id"] == "loc-big"
    readback = await EvidenceReader(store).read(
        pointer["evidence_id"], "run", engagement_id="eng", query="needle-XYZ"
    )
    assert readback["status"] == "ok" and "needle-XYZ" in readback["text"]


async def test_error_only_missing_and_foreign_bodies_are_never_elided():
    store = await _store(
        locals_=[
            _local(
                "loc-e0",
                {"ok": False, "error": "boom 한글", "payload": {"diagnostic": {"code": "demo"}}},
            )
        ]
    )
    error_only = {
        "ok": False,
        "error": "boom 한글",
        "evidence_ids": ["loc-e0"],
        "payload": {"diagnostic": {"code": "demo"}},
    }
    missing = {
        "ok": True,
        "payload": {"status": "ok", "truncated": True},
        "evidence_read_request": {
            "evidence_id": "ev-missing",
            "offset": 0,
            "limit": 4000,
            "query": None,
        },
        "evidence_ids": ["loc-e0"],
    }
    foreign = {"ok": True, "executed": True, "evidence_ids": ["ev-9"]}
    messages = [
        *_round("c0", error_only),
        *_round("c1", missing),
        *_round("c2", foreign),
        *_executed("c3", "ev-1"),
    ]
    delivered = _tool_bodies(await _deliver(store, messages))
    assert delivered[0] == error_only
    assert "elided" not in delivered[1] and delivered[1]["payload"]["status"] == "not_found"
    assert "elided" not in delivered[2]
    assert delivered[2]["original_results"][0]["status"] == "run_mismatch"


async def test_latest_two_user_turns_are_exact_delivery():
    store = await _store(locals_=[_local("loc-t", {"ok": True})])
    old_round = _round("c0", await _stored_read(store, "ev-0", "loc-t"))
    tail_read = await _stored_read(store, "ev-1", "loc-t", limit=2000)
    messages = [*old_round, *_round("c1", tail_read), *_executed("c2", "ev-2")]
    delivered = await _deliver(store, messages)
    expected_window = await EvidenceReader(store).read(
        evidence_id="ev-1", run_id="run", engagement_id="eng", offset=0, limit=2000
    )
    expected = json.dumps(
        {
            **tail_read,
            "payload": redact_secret_material(expected_window),
            "delivery": {"requested_limit": 2000, "delivered_limit": 2000},
        },
        ensure_ascii=False,
    )
    assert delivered[len(old_round) + 2] == {**messages[len(old_round) + 2], "content": expected}
    tail = _tool_bodies(delivered)[-1]
    assert tail["original_results"][0]["offset"] == 0 and "elided" not in tail


async def test_input_pairing_and_checkpoint_round_trip_are_unchanged():
    store = await _store(locals_=[_local("loc-c", {"ok": True})])
    history = [
        *_round("c0", await _stored_read(store, "ev-0", "loc-c")),
        *_executed("c1", "ev-2"),
        *_executed("c2", "ev-3"),
    ]
    history[1]["provider_continuation"] = [{"type": "thinking", "signature": "sig-abc"}]
    original = copy.deepcopy(history)
    delivered = await _deliver(store, history)
    assert history == original
    assert [m["tool_call_id"] for m in delivered if m["role"] == "tool"] == ["c0", "c1", "c2"]
    assert delivered[1]["provider_continuation"] == original[1]["provider_continuation"]
    saver = build_checkpointer(enabled=True)
    restored = saver.serde.loads_typed(saver.serde.dumps_typed({"native_messages": history}))
    assert json.dumps(
        await _deliver(store, restored["native_messages"]), ensure_ascii=False
    ) == json.dumps(delivered, ensure_ascii=False)
    assert SECRET not in json.dumps(delivered)


async def test_delivery_is_deterministic_and_reloaded_elided_copies_are_not_rehydrated():
    store = await _store(locals_=[_local("loc-d", {"ok": True})])
    history = [
        *_round("c0", await _stored_read(store, "ev-0", "loc-d")),
        *_executed("c1", "ev-2"),
        *_executed("c2", "ev-3"),
    ]
    first = await _deliver(store, history)
    assert first == await _deliver(store, history)
    again = await _deliver(store, first)
    assert json.dumps(again, ensure_ascii=False) == json.dumps(first, ensure_ascii=False)


async def test_store_reads_match_the_validation_and_tail_expectation():
    store = await _store(locals_=[_local("loc-n", {"ok": True})])
    history = [
        *_round("c0", await _stored_read(store, "ev-0", "loc-n")),
        *_executed("c1", "ev-2"),
        *_executed("c2", "ev-3"),
        *_executed("c3", "ev-4"),
    ]
    store.gets = 0
    await _deliver(store, history)
    # Historic explicit read: local envelope + target; historic original: one preview.
    # Tail: two original preview windows (one lookup each).
    assert store.gets <= 2 + 1 + 2 + 2


async def _l1_oracle(history, store):
    """Previous delivery rules, written independently: explicit windows and first-seen previews."""
    reader = EvidenceReader(store)
    sent: set[str] = set()
    out = []
    for message in history:
        if message["role"] != "tool":
            out.append(message)
            continue
        body = json.loads(message["content"])
        if body.get("evidence_read_request"):
            req = body["evidence_read_request"]
            limit = min(req["limit"], READ_DELIVERY_MAX_CHARS)
            window = await reader.read(
                evidence_id=req["evidence_id"],
                run_id="run",
                engagement_id="eng",
                offset=req["offset"],
                limit=limit,
                query=req["query"],
            )
            out.append(
                {
                    **message,
                    "content": json.dumps(
                        {
                            **body,
                            "payload": redact_secret_material(window),
                            "delivery": {"requested_limit": req["limit"], "delivered_limit": limit},
                        },
                        ensure_ascii=False,
                    ),
                }
            )
        elif body.get("executed") is True:
            windows = []
            for ref in body["evidence_ids"]:
                if ref in sent:
                    continue
                sent.add(ref)
                windows.append(
                    redact_secret_material(
                        await reader.read(
                            evidence_id=ref,
                            run_id="run",
                            engagement_id="eng",
                            offset=0,
                            limit=delivery.ORIGINAL_PREVIEW_CHARS,
                        )
                    )
                )
            out.append(
                {
                    **message,
                    "content": json.dumps(
                        {
                            **body,
                            "original_results": windows,
                            "original_results_note": delivery._ORIGINAL_NOTE,
                        },
                        ensure_ascii=False,
                    ),
                }
            )
        else:
            out.append(message)
    return out


async def test_historic_elision_reduces_utf8_bytes_on_a_synthetic_history():
    store = await _store(locals_=[_local("loc-s", {"ok": True})])
    history = []
    for index in range(12):
        history += _round(f"r{index}", await _stored_read(store, "ev-0", "loc-s"))
        history += _executed(f"e{index}", "ev-2")
    delivered = await _deliver(store, history)
    oracle = await _l1_oracle(history, store)
    assert _size(delivered) < _size(oracle)
    assert delivered[-3:] == oracle[-3:]


async def test_redelivery_of_an_elided_body_keeps_its_exact_reread_window():
    store = await _store(locals_=[_local("loc-m", {"ok": True})])
    body = await _stored_read(store, "ev-0", "loc-m", offset=10, limit=800, query="marker")
    history = [*_round("c0", body), *_executed("c1", "ev-2"), *_executed("c2", "ev-3")]
    first = await _deliver(store, history)
    old = _tool_bodies(first)[0]
    assert old["reread"] == {"evidence_id": "ev-0", "offset": 10, "limit": 800, "query": "marker"}
    again = await _deliver(store, first)
    assert json.dumps(again, ensure_ascii=False) == json.dumps(first, ensure_ascii=False)
    assert _tool_bodies(again)[0]["reread"] == old["reread"]
