"""Persistence for the Operator Intent history.

Two backends behind one ``OperatorIntentStore`` Protocol: an in-memory
store (default; zero-config, used by unit tests) and a SQLite store
(durable across process restarts). Both keep an append-only history —
an applied or rejected intent is never silently rewritten back to
``received``, so the audit trail cannot be edited by a later turn.

The history holds only redacted instructions (see ``redaction``); this
module adds no secret handling of its own.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from typing import Protocol

from redteam.core.contracts.mission import redact_secret_material
from redteam.core.ids import MissionId, OperatorIntentId
from redteam.orchestration.operator_intents.models import IntentRecord, IntentStatus


class OperatorIntentStore(Protocol):
    """Append-only operator-intent history."""

    async def append(self, record: IntentRecord) -> None: ...
    async def update(self, record: IntentRecord) -> None: ...
    async def get(self, intent_id: OperatorIntentId) -> IntentRecord | None: ...
    async def list_for_mission(
        self,
        mission_id: MissionId,
        *,
        status: IntentStatus | None = None,
        limit: int | None = None,
    ) -> list[IntentRecord]: ...


class DuplicateIntentError(ValueError):
    pass


class IntentNotFoundError(ValueError):
    pass


def _sort_key(record: IntentRecord) -> tuple[object, str]:
    return (record.intent.created_at, str(record.intent.intent_id))


def _safe_record(record: IntentRecord | dict) -> IntentRecord:
    """Sanitize even model_copy-updated records before storing or returning."""
    payload = record.model_dump(mode="json") if isinstance(record, IntentRecord) else record
    return IntentRecord.model_validate(redact_secret_material(payload))


class InMemoryOperatorIntentStore:
    """Concurrency-safe in-memory intent history."""

    def __init__(self) -> None:
        self._records: dict[OperatorIntentId, IntentRecord] = {}
        self._lock = asyncio.Lock()

    async def append(self, record: IntentRecord) -> None:
        async with self._lock:
            if record.intent.intent_id in self._records:
                raise DuplicateIntentError(f"intent exists: {record.intent.intent_id}")
            self._records[record.intent.intent_id] = _safe_record(record)

    async def update(self, record: IntentRecord) -> None:
        async with self._lock:
            if record.intent.intent_id not in self._records:
                raise IntentNotFoundError(f"intent not found: {record.intent.intent_id}")
            self._records[record.intent.intent_id] = _safe_record(record)

    async def get(self, intent_id: OperatorIntentId) -> IntentRecord | None:
        async with self._lock:
            record = self._records.get(intent_id)
            return _safe_record(record) if record is not None else None

    async def list_for_mission(
        self,
        mission_id: MissionId,
        *,
        status: IntentStatus | None = None,
        limit: int | None = None,
    ) -> list[IntentRecord]:
        async with self._lock:
            records = [
                _safe_record(record)
                for record in self._records.values()
                if record.intent.mission_id == mission_id
                and (status is None or record.status is status)
            ]
        records.sort(key=_sort_key)
        if limit is not None:
            return records[-limit:] if limit > 0 else []
        return records


class SQLiteOperatorIntentStore:
    """Durable intent history; same shape as the Task Graph store."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._memory = InMemoryOperatorIntentStore()
        self._ready = False
        self._lock = asyncio.Lock()

    async def setup(self) -> None:
        if self._ready:
            return
        async with self._lock:
            if self._ready:
                return
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(self._path) as db:
                db.executescript(
                    """
                    PRAGMA journal_mode=WAL;
                    CREATE TABLE IF NOT EXISTS operator_intents (
                        intent_id TEXT PRIMARY KEY,
                        mission_id TEXT NOT NULL,
                        status TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        payload TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_operator_intents_mission
                        ON operator_intents(mission_id, status, created_at, intent_id);
                    """
                )
                records = [
                    _safe_record(json.loads(row[0]))
                    for row in db.execute("SELECT payload FROM operator_intents")
                ]
            self._memory._records = {record.intent.intent_id: record for record in records}
            self._ready = True

    def _flush(self) -> None:
        with sqlite3.connect(self._path) as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM operator_intents")
            db.executemany(
                "INSERT INTO operator_intents VALUES(?,?,?,?,?)",
                [
                    (
                        str(record.intent.intent_id),
                        str(record.intent.mission_id),
                        record.status.value,
                        record.intent.created_at.isoformat(),
                        _safe_record(record).model_dump_json(),
                    )
                    for record in self._memory._records.values()
                ],
            )
            db.commit()

    async def _mutate(self, method: str, *args, **kwargs):
        await self.setup()
        async with self._lock:
            result = await getattr(self._memory, method)(*args, **kwargs)
            self._flush()
            return result

    async def append(self, record: IntentRecord) -> None:
        await self._mutate("append", record)

    async def update(self, record: IntentRecord) -> None:
        await self._mutate("update", record)

    async def get(self, intent_id: OperatorIntentId) -> IntentRecord | None:
        await self.setup()
        return await self._memory.get(intent_id)

    async def list_for_mission(
        self,
        mission_id: MissionId,
        *,
        status: IntentStatus | None = None,
        limit: int | None = None,
    ) -> list[IntentRecord]:
        await self.setup()
        return await self._memory.list_for_mission(mission_id, status=status, limit=limit)
