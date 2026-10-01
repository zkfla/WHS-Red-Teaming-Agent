"""Event payload safety without changing event consumer field names."""

from datetime import datetime, timezone

from redteam.core.contracts.events import EventEnvelope, EventType
from redteam.core.ids import EngagementId, EventId, RunId


def test_event_envelope_redacts_free_text_and_nested_secret_values() -> None:
    event = EventEnvelope(
        event_id=EventId("evt_secret"),
        event_type=EventType.TASK_BLOCKED,
        run_id=RunId("run_secret"),
        engagement_id=EngagementId("eng_secret"),
        source="orchestration",
        timestamp=datetime.now(timezone.utc),
        correlation_id="intent_1",
        causation_id="evt_prev",
        payload={
            "reason": "waiting for token=private-value",
            "details": [{"note": "Bearer another-private-value", "password": "raw-password"}],
            "access_token": "opaque-raw-token",
            "credential_ref": "vault://ref-1",
            "task_id": "task_1",
        },
    )

    serialized = event.model_dump_json()
    assert "private-value" not in serialized
    assert "raw-password" not in serialized
    assert "opaque-raw-token" not in serialized
    assert "REDACTED" in serialized
    assert event.payload["task_id"] == "task_1"
    assert event.payload["credential_ref"] == "vault://ref-1"
    assert event.correlation_id == "intent_1"
    assert event.causation_id == "evt_prev"
