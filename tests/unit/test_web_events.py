"""Event log ordering, SSE replay, masking and the public turn fields."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from redteam.app.web.events import (
    EventLog,
    event_stream,
    public_payload,
    runtime_event_data,
)


def fill(log: EventLog, count: int) -> None:
    for number in range(count):
        log.append("event", {"n": number})


def test_sequence_is_monotonic_and_tail_is_bounded():
    log = EventLog(capacity=5)
    fill(log, 8)
    assert log.last_seq == 8
    assert [item["seq"] for item in log.tail()] == [4, 5, 6, 7, 8]


def test_since_returns_items_after_the_cursor_or_none_on_a_gap():
    log = EventLog(capacity=5)
    fill(log, 8)
    assert [item["seq"] for item in log.since(6)] == [7, 8]
    assert log.since(8) == []
    assert log.since(3) == [item for item in log.tail() if item["seq"] > 3]
    assert log.since(2) is None  # seq 3 was dropped: gap cannot be reproduced
    assert log.since(99) is None  # cursor from an earlier server instance


def test_public_payload_masks_secret_keys_and_bounds_size():
    payload = {
        "api_key": "sk-secret",
        "nested": {"token": "abc", "ok": "visible"},
        "long": "x" * 5000,
        "many": list(range(100)),
    }
    cleaned = public_payload("ToolExecutionFinished", payload)
    assert cleaned["api_key"] == "***REDACTED***"
    assert cleaned["nested"]["token"] == "***REDACTED***"
    assert cleaned["nested"]["ok"] == "visible"
    assert len(cleaned["long"]) < 700
    assert len(cleaned["many"]) == 20


def test_agent_turn_exposes_only_public_fields():
    payload = {
        "response": "공개 응답",
        "decision_summary": "요약",
        "assumptions": ["a", "b", "c", "d"],
        "reasoning": "private chain of thought",
        "raw_prompt": "system prompt",
    }
    cleaned = public_payload("AgentTurnDecided", payload)
    assert set(cleaned) == {"response", "decision_summary", "assumptions"}
    assert cleaned["assumptions"] == ["a", "b", "c"]


def test_runtime_event_data_carries_run_id_and_masked_payload():
    event = SimpleNamespace(
        event_type="ActionProposed",
        event_id="evt_1",
        run_id="run_1",
        timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
        source="orchestration",
        payload={"operation": "http_request", "password": "hunter2"},
    )
    data, run_id = runtime_event_data(event)
    assert run_id == "run_1"
    assert data["payload"]["password"] == "***REDACTED***"
    assert data["payload"]["operation"] == "http_request"


async def collect(stream, count: int):
    items = []
    async for message in stream:
        items.append(message)
        if len(items) == count:
            break
    return items


async def test_stream_replays_backlog_then_delivers_live_items_once():
    log = EventLog()
    fill(log, 3)
    closing = asyncio.Event()
    stream = event_stream(log, after=1, closing=closing, heartbeat=0.05)
    first = await collect(stream, 2)
    assert [message["id"] for message in first] == ["2", "3"]
    log.append("state", {"phase": "running"})
    nxt = await asyncio.wait_for(anext(stream), 1)
    assert nxt["id"] == "4" and nxt["event"] == "state"
    assert json.loads(nxt["data"])["data"] == {"phase": "running"}
    closing.set()
    await stream.aclose()


async def test_stream_without_cursor_starts_live_only():
    log = EventLog()
    fill(log, 3)
    closing = asyncio.Event()
    stream = event_stream(log, after=None, closing=closing, heartbeat=0.05)
    waiter = asyncio.create_task(anext(stream))
    await asyncio.sleep(0.01)
    log.append("event", {"n": 99})
    message = await asyncio.wait_for(waiter, 1)
    assert message["id"] == "4"
    closing.set()
    await stream.aclose()


async def test_stream_signals_reset_when_the_gap_is_unrecoverable():
    log = EventLog(capacity=3)
    fill(log, 10)
    stream = event_stream(log, after=2, closing=asyncio.Event(), heartbeat=0.05)
    first = await anext(stream)
    assert first["event"] == "reset"
    assert json.loads(first["data"])["last_seq"] == 10
    with pytest.raises(StopAsyncIteration):
        await anext(stream)


async def test_slow_subscriber_is_reset_instead_of_dropping_items_silently(monkeypatch):
    monkeypatch.setattr("redteam.app.web.events.SUBSCRIBER_QUEUE", 2)
    log = EventLog()
    closing = asyncio.Event()
    stream = event_stream(log, after=None, closing=closing, heartbeat=0.05)
    waiter = asyncio.create_task(anext(stream))
    await asyncio.sleep(0.01)
    fill(log, 10)  # overflows the 2-slot queue before the consumer runs
    message = await asyncio.wait_for(waiter, 1)
    assert message["event"] in {"reset", "event"}
    seen = [message]
    while message["event"] != "reset":
        message = await asyncio.wait_for(anext(stream), 1)
        seen.append(message)
    assert seen[-1]["event"] == "reset"


async def test_stream_ends_when_the_host_is_closing():
    log = EventLog()
    closing = asyncio.Event()
    stream = event_stream(log, after=None, closing=closing, heartbeat=0.02)
    waiter = asyncio.create_task(anext(stream))
    await asyncio.sleep(0.03)
    closing.set()
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(waiter, 1)
    assert not log._subscribers
