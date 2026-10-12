"""Bounded, ordered, masked event log shared by every browser connection.

The log is a display projection of the live session (runtime events, input
receipts, session state). It is not a second source of domain state: the
central database, evidence and approval ledgers stay the originals, and the
snapshot is rebuilt from the controller plus this bounded window.
"""

from __future__ import annotations

import asyncio
import json
from collections import deque
from datetime import datetime, timezone
from typing import Any

from redteam.app.cli._shared import mask_secrets
from redteam.app.cli.display import safe_text

SCHEMA_VERSION = 1
LOG_CAPACITY = 2000
SUBSCRIBER_QUEUE = 500
TEXT_LIMIT = 600
LIST_LIMIT = 20
KEY_LIMIT = 40
DEPTH_LIMIT = 4

# Only these fields of the agent's turn decision are public. Reasoning stays out.
_PUBLIC_TURN_FIELDS = ("response", "decision_summary")


def _clean(value: Any, depth: int) -> Any:
    if depth > DEPTH_LIMIT:
        return "…"
    if isinstance(value, dict):
        return {
            safe_text(str(key), limit=80): _clean(item, depth + 1)
            for key, item in list(value.items())[:KEY_LIMIT]
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_clean(item, depth + 1) for item in list(value)[:LIST_LIMIT]]
    if isinstance(value, bool) or value is None or isinstance(value, (int, float)):
        return value
    return safe_text(value, limit=TEXT_LIMIT)


def public_payload(event_type: str, payload: Any) -> dict[str, Any]:
    """Masked, size-bounded copy of an event payload that is safe to display."""
    if not isinstance(payload, dict):
        return {"value": _clean(payload, 0)}
    if event_type == "AgentTurnDecided":
        kept: dict[str, Any] = {
            key: payload[key] for key in _PUBLIC_TURN_FIELDS if payload.get(key)
        }
        assumptions = payload.get("assumptions")
        if isinstance(assumptions, list):
            kept["assumptions"] = assumptions[:3]
        payload = kept
    return _clean(mask_secrets(payload), 0)


def runtime_event_data(event: Any) -> tuple[dict[str, Any], str]:
    """DTO body for one ``EventEnvelope`` and its run id."""
    event_type = str(getattr(event, "event_type", ""))
    stamp: Any = getattr(event, "timestamp", None)
    data: dict[str, Any] = {
        "event_type": safe_text(event_type, limit=80),
        "event_id": safe_text(getattr(event, "event_id", ""), limit=120),
        "ts": stamp.isoformat() if hasattr(stamp, "isoformat") else None,
        "source": safe_text(getattr(event, "source", ""), limit=80),
        "payload": public_payload(event_type, getattr(event, "payload", {})),
    }
    return data, str(getattr(event, "run_id", "") or "")


class Subscription:
    def __init__(self, log: EventLog) -> None:
        self._log = log
        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(SUBSCRIBER_QUEUE)
        self.overflow = False
        self.closed = False

    def offer(self, item: dict[str, Any]) -> None:
        if self.closed:
            return
        try:
            self._queue.put_nowait(item)
        except asyncio.QueueFull:
            # The client must resynchronise from a snapshot; never drop silently.
            self.overflow = True
            self.close()

    async def get(self, timeout: float) -> dict[str, Any] | None:
        try:
            return await asyncio.wait_for(self._queue.get(), timeout)
        except asyncio.TimeoutError:
            return None

    def close(self) -> None:
        self.closed = True
        self._log._subscribers.discard(self)


class EventLog:
    def __init__(self, capacity: int = LOG_CAPACITY) -> None:
        self._items: deque[dict[str, Any]] = deque(maxlen=capacity)
        self._seq = 0
        self._subscribers: set[Subscription] = set()

    @property
    def last_seq(self) -> int:
        return self._seq

    def append(
        self, kind: str, data: dict[str, Any], *, run_id: str | None = None
    ) -> dict[str, Any]:
        self._seq += 1
        item = {
            "seq": self._seq,
            "kind": kind,
            "run_id": run_id or None,
            "ts": datetime.now(timezone.utc).isoformat(),
            "data": data,
        }
        self._items.append(item)
        for subscription in list(self._subscribers):
            subscription.offer(item)
        return item

    def tail(self, count: int = 1000) -> list[dict[str, Any]]:
        return list(self._items)[-count:]

    def since(self, seq: int) -> list[dict[str, Any]] | None:
        """Items after ``seq``, or ``None`` when the gap cannot be reproduced."""
        if seq > self._seq:
            return None  # the client saw an earlier server instance
        if seq == self._seq:
            return []
        oldest = self._items[0]["seq"] if self._items else self._seq + 1
        if seq < oldest - 1:
            return None
        return [item for item in self._items if item["seq"] > seq]

    def subscribe(self) -> Subscription:
        subscription = Subscription(self)
        self._subscribers.add(subscription)
        return subscription


def sse_message(item: dict[str, Any]) -> dict[str, str]:
    return {"event": item["kind"], "id": str(item["seq"]), "data": json.dumps(item)}


async def event_stream(
    log: EventLog,
    *,
    after: int | None,
    closing: asyncio.Event,
    heartbeat: float = 15.0,
):
    """Backlog after ``after`` then live items; ends with ``reset`` on a gap.

    The subscription is opened before the backlog is read, so an item appended
    in between is delivered once (de-duplicated by ``seq``).
    """
    subscription = log.subscribe()
    try:
        seen = log.last_seq if after is None else after
        if after is not None:
            backlog = log.since(after)
            if backlog is None:
                yield {"event": "reset", "data": json.dumps({"last_seq": log.last_seq})}
                return
            for item in backlog:
                seen = item["seq"]
                yield sse_message(item)
        while not closing.is_set():
            live = await subscription.get(heartbeat)
            if subscription.overflow:
                yield {"event": "reset", "data": json.dumps({"last_seq": log.last_seq})}
                return
            if live is None or live["seq"] <= seen:
                continue
            seen = live["seq"]
            yield sse_message(live)
    finally:
        subscription.close()
