"""Same-run, leased, at-least-once delivery of committed envelopes."""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable, Coroutine
from typing import Any, Protocol

from redteam.core.contracts.events import EventEnvelope
from redteam.orchestration.coordination import CoordinationError, Coordinator


class OutboxSession(Protocol):
    records: dict[tuple[str, str], Any]


OutboxChange = Callable[[OutboxSession], Coroutine[Any, Any, Any]]


def pending_count(change: OutboxSession, run_id: str) -> int:
    """One durable undelivered projection, including leased/failed deliveries."""
    return sum(
        kind == "outbox" and not row["delivered"] and row["envelope"]["run_id"] == run_id
        for (kind, _), row in change.records.items()
    )


async def deliver_pending(
    coordinator: Coordinator | None = None,
    *,
    run_id: str,
    engagement_id: str,
    event_bus: Any,
    budget_seconds: float = 2.0,
    change: Callable[[OutboxChange], Awaitable[Any]] | None = None,
) -> int:
    """Return undelivered count; publishing never holds a storage lock."""
    owner = uuid.uuid4().hex
    deadline = time.monotonic() + budget_seconds

    if (coordinator is None) == (change is None):
        raise ValueError("select one outbox transaction boundary")

    async def change_records(callback: OutboxChange) -> Any:
        if change is not None:
            return await change(callback)
        assert coordinator is not None

        async def legacy(tx):
            return await callback(tx)

        return await coordinator.change(legacy)

    async def claim(change: OutboxSession) -> list[EventEnvelope]:
        binding = change.records.get(("binding", run_id))
        if binding is None or binding["engagement_id"] != engagement_id:
            raise CoordinationError("outbox_binding_mismatched")
        now = time.time()
        batch: list[EventEnvelope] = []
        rows = [
            row
            for (kind, _), row in change.records.items()
            if kind == "outbox"
            and not row["delivered"]
            and row["envelope"]["run_id"] == run_id
            and row["envelope"]["engagement_id"] == engagement_id
        ]
        rows.sort(key=lambda row: (row.get("sequence", 0), row["envelope"]["timestamp"]))
        for row in rows:
            raw = row["envelope"]
            if row["owner"] is not None and row["lease_until"] > now:
                break
            row.update(owner=owner, lease_until=now + 10.0)
            batch.append(EventEnvelope.model_validate(raw))
            if len(batch) == 200:
                break
        return batch

    async def acknowledge(change: OutboxSession, event: EventEnvelope, *, delivered: bool) -> bool:
        row = change.records.get(("outbox", str(event.event_id)))
        if row is not None and row["owner"] == owner:
            if delivered:
                row.update(delivered=True, owner=None, lease_until=0.0)
            else:
                row.update(lease_until=time.time() + 10.0)
            return True
        return False

    async def count(change: OutboxSession) -> int:
        return pending_count(change, run_id)

    if event_bus is not None:
        while time.monotonic() < deadline:
            batch = await change_records(claim)
            if not batch:
                break
            for event in batch:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return await change_records(count)

                # Renew ownership before each publication; another worker cannot
                # claim this batch while its current lease is valid.
                async def renew(change: OutboxSession, event: EventEnvelope = event) -> bool:
                    return await acknowledge(change, event, delivered=False)

                owned = await change_records(renew)
                if not owned:
                    return await change_records(count)
                try:
                    await asyncio.wait_for(event_bus.publish(event), timeout=remaining)
                except Exception:  # noqa: BLE001  # delivery is recoverable, never roll back effect
                    return await change_records(count)

                async def ack(change: OutboxSession, event: EventEnvelope = event) -> bool:
                    return await acknowledge(change, event, delivered=True)

                await change_records(ack)
    return await change_records(count)
