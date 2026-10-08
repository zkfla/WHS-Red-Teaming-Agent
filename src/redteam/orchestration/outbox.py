"""Same-run, leased, at-least-once delivery of committed envelopes."""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from redteam.core.contracts.events import EventEnvelope
from redteam.orchestration.coordination import (
    ChangeSet,
    CoordinationError,
    Coordinator,
    content_hash,
)


async def consume_pending(
    coordinator: Coordinator,
    *,
    run_id: str,
    engagement_id: str,
    consumer_id: str,
    event_types: frozenset[str],
    handler: Callable[[EventEnvelope], Awaitable[None]],
    lease_seconds: float = 10.0,
    max_events: int = 200,
) -> None:
    """Recoverable internal consumption using the existing committed outbox.

    Delivery to the public bus and this consumer have independent cursors.
    Effects must be idempotent: a crash after an effect but before ack replays
    that effect. No transport/LLM/tool call occurs under the state lock.
    An occupied/expired lease fails closed rather than admitting stale work.
    """
    if not consumer_id.strip() or lease_seconds <= 0 or max_events < 1:
        raise ValueError("invalid_outbox_consumer_limits")
    owner = uuid.uuid4().hex
    key = ("consumer_cursor", f"{run_id}:{consumer_id}")

    async def claim(change: ChangeSet):
        binding = change.records.get(("binding", run_id))
        if binding is not None and binding["engagement_id"] != engagement_id:
            raise CoordinationError("outbox_consumer_binding_mismatched")
        cursor = change.records.setdefault(
            key,
            {
                "sequence": 0,
                "owner": None,
                "lease_until": 0.0,
            },
        )
        now = time.time()
        if cursor["owner"] is not None and cursor["lease_until"] > now:
            raise CoordinationError("outbox_consumer_busy")
        rows = sorted(
            (
                row
                for (kind, _), row in change.records.items()
                if kind == "outbox"
                and row["sequence"] > cursor["sequence"]
                and row["envelope"]["run_id"] == run_id
                and row["envelope"]["engagement_id"] == engagement_id
                and row["envelope"]["event_type"] in event_types
            ),
            key=lambda row: row["sequence"],
        )
        if not rows:
            return None
        row = rows[0]
        event = EventEnvelope.model_validate(row["envelope"])
        dedup = str(event.payload.get("dedup_key") or event.event_id)
        effect_key = ("consumer_effect", f"{run_id}:{consumer_id}:{dedup}")
        if effect_key in change.records:
            if change.records[effect_key]["payload_sha256"] != content_hash(
                {
                    "event_type": event.event_type,
                    "payload": event.payload,
                }
            ):
                raise CoordinationError("outbox_consumer_dedup_conflict")
            cursor.update(sequence=row["sequence"], owner=None, lease_until=0.0)
            return (event, row["sequence"], effect_key, True)
        cursor.update(owner=owner, lease_until=now + lease_seconds)
        return (event, row["sequence"], effect_key, False)

    for _ in range(max_events):
        claimed = await coordinator.change(claim)
        if claimed is None:
            return
        event, sequence, effect_key, replay = claimed
        if replay:
            continue
        try:
            await asyncio.wait_for(handler(event), timeout=lease_seconds)
        except BaseException:

            async def release(change: ChangeSet):
                cursor = change.records[key]
                if cursor["owner"] == owner:
                    cursor.update(owner=None, lease_until=0.0)

            await coordinator.change(release)
            raise

        async def acknowledge(
            change: ChangeSet,
            effect_key: tuple[str, str] = effect_key,
            event: EventEnvelope = event,
            sequence: int = sequence,
        ):
            cursor = change.records[key]
            if cursor["owner"] != owner or cursor["lease_until"] <= time.time():
                raise CoordinationError("outbox_consumer_lease_lost")
            change.records[effect_key] = {
                "event_id": str(event.event_id),
                "sequence": sequence,
                "payload_sha256": content_hash(
                    {
                        "event_type": event.event_type,
                        "payload": event.payload,
                    }
                ),
            }
            cursor.update(sequence=sequence, owner=None, lease_until=0.0)

        await coordinator.change(acknowledge)
    # Never silently run against an incompletely reconciled frontier.
    if await coordinator.change(claim) is not None:

        async def release_tail(change: ChangeSet):
            cursor = change.records[key]
            if cursor["owner"] == owner:
                cursor.update(owner=None, lease_until=0.0)

        await coordinator.change(release_tail)
        raise CoordinationError("outbox_consumer_batch_exhausted")


async def deliver_pending(
    coordinator: Coordinator,
    *,
    run_id: str,
    engagement_id: str,
    event_bus: Any,
    budget_seconds: float = 2.0,
) -> int:
    """Return undelivered count; publishing never holds a storage lock."""
    owner = uuid.uuid4().hex
    deadline = time.monotonic() + budget_seconds

    async def claim(change: ChangeSet) -> list[EventEnvelope]:
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

    async def acknowledge(change: ChangeSet, event: EventEnvelope, *, delivered: bool) -> bool:
        row = change.records.get(("outbox", str(event.event_id)))
        if row is not None and row["owner"] == owner:
            if delivered:
                row.update(delivered=True, owner=None, lease_until=0.0)
            else:
                row.update(lease_until=time.time() + 10.0)
            return True
        return False

    async def count(change: ChangeSet) -> int:
        return sum(
            kind == "outbox" and not row["delivered"] and row["envelope"]["run_id"] == run_id
            for (kind, _), row in change.records.items()
        )

    if event_bus is not None:
        while time.monotonic() < deadline:
            batch = await coordinator.change(claim)
            if not batch:
                break
            for event in batch:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return await coordinator.change(count)

                # Renew ownership before each publication; another worker cannot
                # claim this batch while its current lease is valid.
                async def renew(change: ChangeSet, event: EventEnvelope = event) -> bool:
                    return await acknowledge(change, event, delivered=False)

                owned = await coordinator.change(renew)
                if not owned:
                    return await coordinator.change(count)
                try:
                    await asyncio.wait_for(event_bus.publish(event), timeout=remaining)
                except Exception:  # noqa: BLE001  # delivery is recoverable, never roll back effect
                    return await coordinator.change(count)

                async def ack(change: ChangeSet, event: EventEnvelope = event) -> bool:
                    return await acknowledge(change, event, delivered=True)

                await coordinator.change(ack)
    return await coordinator.change(count)
