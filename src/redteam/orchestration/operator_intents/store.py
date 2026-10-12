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
from redteam.sqlite_support import connect, run_sqlite, run_transaction


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


def sanitize_intent_record(record: IntentRecord | dict) -> IntentRecord:
    """Sanitize even model_copy-updated records before storing or returning."""
    payload = record.model_dump(mode="json") if isinstance(record, IntentRecord) else record
    return IntentRecord.model_validate(redact_secret_material(payload))


class IntentIdentityConflictError(ValueError):
    """An existing intent identifier cannot be reused with another instruction or run."""


def check_intent_update(old: IntentRecord, record: IntentRecord) -> IntentRecord:
    record = sanitize_intent_record(record)
    if old.intent != record.intent or old.correlation_id != record.correlation_id:
        raise IntentIdentityConflictError("intent identity and content are immutable")
    if old.run_id is not None and old.run_id != record.run_id:
        raise IntentIdentityConflictError("intent run binding is immutable")
    if old.status in {IntentStatus.APPLIED, IntentStatus.REJECTED}:
        # The history is a projection: a delayed RECEIVED copy cannot erase a result.
        return old
    return record


class InMemoryOperatorIntentStore:
    """Concurrency-safe in-memory intent history."""

    def __init__(self) -> None:
        self._records: dict[OperatorIntentId, IntentRecord] = {}
        self._lock = asyncio.Lock()

    async def append(self, record: IntentRecord) -> None:
        async with self._lock:
            if record.intent.intent_id in self._records:
                raise DuplicateIntentError(f"intent exists: {record.intent.intent_id}")
            self._records[record.intent.intent_id] = sanitize_intent_record(record)

    async def update(self, record: IntentRecord) -> None:
        async with self._lock:
            if record.intent.intent_id not in self._records:
                raise IntentNotFoundError(f"intent not found: {record.intent.intent_id}")
            self._records[record.intent.intent_id] = check_intent_update(
                self._records[record.intent.intent_id], record
            )

    async def get(self, intent_id: OperatorIntentId) -> IntentRecord | None:
        async with self._lock:
            record = self._records.get(intent_id)
            return sanitize_intent_record(record) if record is not None else None

    async def list_for_mission(
        self,
        mission_id: MissionId,
        *,
        status: IntentStatus | None = None,
        limit: int | None = None,
    ) -> list[IntentRecord]:
        async with self._lock:
            records = [
                sanitize_intent_record(record)
                for record in self._records.values()
                if record.intent.mission_id == mission_id
                and (status is None or record.status is status)
            ]
        records.sort(key=_sort_key)
        if limit is not None:
            return records[-limit:] if limit > 0 else []
        return records


class SQLiteOperatorIntentStore:
    """Live row-based audit projection, durable across independent service instances."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._ready = False
        self._lock = asyncio.Lock()

    def _connect(self) -> sqlite3.Connection:
        return connect(self._path)

    async def setup(self) -> None:
        if self._ready:
            return
        async with self._lock:
            if self._ready:
                return

            def initialize() -> None:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                db = self._connect()
                try:
                    db.execute("PRAGMA journal_mode=WAL")
                    db.execute("BEGIN IMMEDIATE")
                    self._create_schema(db)
                    db.commit()
                except BaseException:
                    db.rollback()
                    raise
                finally:
                    db.close()

            await run_sqlite(initialize)
            self._ready = True

    @staticmethod
    def _create_schema(db: sqlite3.Connection) -> None:
        db.execute(
            """CREATE TABLE IF NOT EXISTS operator_intents (
                intent_id TEXT PRIMARY KEY, mission_id TEXT NOT NULL, status TEXT NOT NULL,
                created_at TEXT NOT NULL, payload TEXT NOT NULL
            )"""
        )
        db.execute(
            """CREATE INDEX IF NOT EXISTS idx_operator_intents_mission
            ON operator_intents(mission_id,status,created_at,intent_id)"""
        )

    @staticmethod
    def _get(db: sqlite3.Connection, intent_id: OperatorIntentId) -> IntentRecord | None:
        row = db.execute(
            "SELECT payload FROM operator_intents WHERE intent_id=?", (str(intent_id),)
        ).fetchone()
        return sanitize_intent_record(json.loads(row[0])) if row else None

    @staticmethod
    def _write(db: sqlite3.Connection, record: IntentRecord) -> None:
        record = sanitize_intent_record(record)
        db.execute(
            """INSERT INTO operator_intents(intent_id,mission_id,status,created_at,payload)
            VALUES(?,?,?,?,?) ON CONFLICT(intent_id) DO UPDATE SET
            status=excluded.status,payload=excluded.payload""",
            (
                str(record.intent.intent_id),
                str(record.intent.mission_id),
                record.status.value,
                record.intent.created_at.isoformat(),
                record.model_dump_json(),
            ),
        )

    async def append(self, record: IntentRecord) -> None:
        await self.setup()
        record = sanitize_intent_record(record)

        def append(db: sqlite3.Connection) -> None:
            if self._get(db, record.intent.intent_id) is not None:
                raise DuplicateIntentError(f"intent exists: {record.intent.intent_id}")
            self._write(db, record)

        await run_transaction(self._path, append, write=True)

    async def update(self, record: IntentRecord) -> None:
        await self.setup()
        record = sanitize_intent_record(record)

        def update(db: sqlite3.Connection) -> None:
            old = self._get(db, record.intent.intent_id)
            if old is None:
                raise IntentNotFoundError(f"intent not found: {record.intent.intent_id}")
            self._write(db, check_intent_update(old, record))

        await run_transaction(self._path, update, write=True)

    async def get(self, intent_id: OperatorIntentId) -> IntentRecord | None:
        await self.setup()
        return await run_transaction(self._path, lambda db: self._get(db, intent_id))

    async def list_for_mission(
        self,
        mission_id: MissionId,
        *,
        status: IntentStatus | None = None,
        limit: int | None = None,
    ) -> list[IntentRecord]:
        await self.setup()

        def read(db: sqlite3.Connection) -> list[IntentRecord]:
            records = [
                sanitize_intent_record(json.loads(row[0]))
                for row in db.execute(
                    "SELECT payload FROM operator_intents WHERE mission_id=?",
                    (str(mission_id),),
                )
            ]
            records = [item for item in records if status is None or item.status is status]
            records.sort(key=_sort_key)
            if limit is not None:
                return records[-limit:] if limit > 0 else []
            return records

        return await run_transaction(self._path, read)
