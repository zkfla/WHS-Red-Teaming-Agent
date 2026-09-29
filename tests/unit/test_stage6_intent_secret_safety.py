"""Operator intent history never persists raw free-text credentials."""

import json
import sqlite3
from datetime import datetime, timezone

import pytest

from redteam.core.contracts.mission import OperatorIntent, Provenance
from redteam.core.enums import OperatorIntentType
from redteam.core.ids import MissionId, OperatorIntentId
from redteam.orchestration.operator_intents.models import IntentRecord
from redteam.orchestration.operator_intents.store import (
    InMemoryOperatorIntentStore,
    SQLiteOperatorIntentStore,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["memory", "sqlite"])
async def test_intent_store_redacts_bypassed_model_copy(backend, tmp_path) -> None:
    path = tmp_path / "intents.sqlite3"
    store = (
        InMemoryOperatorIntentStore() if backend == "memory" else SQLiteOperatorIntentStore(path)
    )
    intent = OperatorIntent(
        intent_id=OperatorIntentId("intent_secret"),
        mission_id=MissionId("mission_secret"),
        intent_type=OperatorIntentType.FOCUS,
        instruction="focus web",
        provenance=Provenance(source="operator"),
        created_at=datetime.now(timezone.utc),
    ).model_copy(update={"instruction": "token=raw-instruction"})
    record = IntentRecord(intent=intent, correlation_id="intent_secret").model_copy(
        update={"reason": "cookie=raw-reason", "outcome": "Bearer raw-outcome"}
    )

    await store.append(record)
    read = await store.get(intent.intent_id)
    assert read is not None
    content = read.model_dump_json()
    assert all(value not in content for value in ("raw-instruction", "raw-reason", "raw-outcome"))
    assert read.intent.intent_id == intent.intent_id
    if backend == "sqlite":
        with sqlite3.connect(path) as db:
            raw = db.execute("SELECT payload FROM operator_intents").fetchone()[0]
        assert "raw-" not in raw


@pytest.mark.asyncio
async def test_legacy_intent_history_is_redacted_on_read_and_next_write(tmp_path) -> None:
    path = tmp_path / "intents.sqlite3"
    store = SQLiteOperatorIntentStore(path)
    await store.setup()
    record = IntentRecord(
        intent=OperatorIntent(
            intent_id=OperatorIntentId("intent_legacy"),
            mission_id=MissionId("mission_secret"),
            intent_type=OperatorIntentType.FOCUS,
            instruction="focus web",
            provenance=Provenance(source="operator"),
            created_at=datetime.now(timezone.utc),
        ),
        correlation_id="intent_legacy",
    )
    payload = record.model_dump(mode="json")
    payload["reason"] = "password=raw-legacy"
    with sqlite3.connect(path) as db:
        db.execute(
            "INSERT INTO operator_intents VALUES(?,?,?,?,?)",
            (
                "intent_legacy",
                "mission_secret",
                "received",
                record.intent.created_at.isoformat(),
                json.dumps(payload),
            ),
        )
    reloaded = SQLiteOperatorIntentStore(path)
    read = await reloaded.get(OperatorIntentId("intent_legacy"))
    assert read is not None and "raw-legacy" not in read.model_dump_json()
    await reloaded.update(read)
    with sqlite3.connect(path) as db:
        assert "raw-legacy" not in db.execute("SELECT payload FROM operator_intents").fetchone()[0]
