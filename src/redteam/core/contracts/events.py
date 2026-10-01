"""EventEnvelope — the one shape every event on the Event Bus uses.

``EventType`` centralizes the canonical ``event_type`` string constants
so producers/consumers never hand-type typos. Backend (in-memory now,
Redis/NATS/Kafka later) lives in ``redteam.event_bus``, decoupled from
this schema via ``redteam.core.ports.event_bus.EventBus``. Event
*implementation* (which nodes actually publish which types) is Agent
Team's job — this vocabulary is fixed by PM.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator

from redteam.core.ids import EngagementId, EventId, RunId
from redteam.core.redaction import SECRET_KEY_PARTS, redact_sensitive_text

_REDACTED = "***REDACTED***"


def _redact_secret_material(value: Any, *, key: str | None = None) -> Any:
    normalized = (key or "").lower().replace("-", "_")
    # A secret-shaped key is the whole key or a trailing segment
    # (``access_token``, ``client_secret``, ``proxy_authorization``). A longer
    # descriptive name that merely contains the word -- such as the
    # diagnostic reason ``no_registered_session_for_path`` -- is not a secret.
    if not normalized.endswith(("_ref", "_reference", "_id")) and any(
        normalized == part or normalized.endswith(f"_{part}")
        for part in SECRET_KEY_PARTS
    ):
        return _REDACTED
    if isinstance(value, dict):
        return {
            item_key: _redact_secret_material(item, key=str(item_key))
            for item_key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_secret_material(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_secret_material(item) for item in value)
    if isinstance(value, str):
        # Domain events also carry free-form policy/task reasons. A token in
        # one of those strings must not survive merely because its key is
        # called "reason" rather than "token".
        return redact_sensitive_text(value)
    return value


class EventEnvelope(BaseModel):
    event_id: EventId

    event_type: str

    run_id: RunId
    engagement_id: EngagementId

    source: str

    timestamp: datetime

    correlation_id: str | None = None
    causation_id: str | None = None

    payload: dict[str, Any] = Field(default_factory=dict)

    @field_validator("payload")
    @classmethod
    def _payload_has_no_raw_secrets(cls, value: dict[str, Any]) -> dict[str, Any]:
        return _redact_secret_material(value)


class EventType:
    """Canonical ``event_type`` values — not a StrEnum on purpose:
    ``EventEnvelope.event_type`` stays a plain str so new event types can
    be added without a schema migration; this class is just the shared
    vocabulary every team should use instead of inventing new literals."""

    RUN_STARTED = "RunStarted"
    RUN_FINISHED = "RunFinished"

    AGENT_STARTED = "AgentStarted"
    AGENT_FINISHED = "AgentFinished"

    HYPOTHESIS_CREATED = "HypothesisCreated"

    ACTION_PROPOSED = "ActionProposed"

    POLICY_EVALUATED = "PolicyEvaluated"
    ACTION_APPROVED = "ActionApproved"
    ACTION_DENIED = "ActionDenied"
    APPROVAL_REQUIRED = "ApprovalRequired"

    TOOL_EXECUTION_STARTED = "ToolExecutionStarted"
    TOOL_EXECUTION_FINISHED = "ToolExecutionFinished"
    TOOL_EXECUTION_FAILED = "ToolExecutionFailed"

    EVIDENCE_CREATED = "EvidenceCreated"

    GRAPH_UPDATED = "GraphUpdated"

    FINDING_CREATED = "FindingCreated"
    FINDING_VERIFIED = "FindingVerified"

    MISSION_CREATED = "MissionCreated"
    MISSION_UPDATED = "MissionUpdated"
    TASK_CREATED = "TaskCreated"
    TASK_STARTED = "TaskStarted"
    TASK_BLOCKED = "TaskBlocked"
    TASK_UNBLOCKED = "TaskUnblocked"
    TASK_COMPLETED = "TaskCompleted"
    TASK_FAILED = "TaskFailed"
    TASK_PRIORITY_CHANGED = "TaskPriorityChanged"
    TASK_CANCELLED = "TaskCancelled"
    OPERATOR_INSTRUCTION_RECEIVED = "OperatorInstructionReceived"
    OPERATOR_INTENT_APPLIED = "OperatorIntentApplied"
    OPERATOR_INTENT_REJECTED = "OperatorIntentRejected"
