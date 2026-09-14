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

from pydantic import BaseModel, Field

from redteam.core.ids import EngagementId, EventId, RunId


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
