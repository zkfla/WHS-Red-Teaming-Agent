"""Runtime models for the Orchestration Operator Intent channel.

The *domain* contract is ``core.contracts.mission.OperatorIntent`` (what
an operator asked for). These models are the Agent Team's runtime view of
that contract: the received/applied/rejected bookkeeping, the event ids
that make the audit chain walkable, and the read-only explanation/status
projections.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field

from redteam.core.contracts.mission import OperatorIntent
from redteam.core.enums import MissionStatus, OperatorIntentType
from redteam.core.ids import EventId, MissionId, OperatorIntentId, RunId, TaskId


class IntentStatus(str, Enum):
    """Lifecycle of one operator instruction inside the runtime."""

    RECEIVED = "received"
    APPLIED = "applied"
    REJECTED = "rejected"


class IntentRecord(BaseModel):
    """One persisted intent plus everything needed to audit its handling."""

    intent: OperatorIntent
    status: IntentStatus = IntentStatus.RECEIVED
    #: Event that recorded the operator's instruction (published at receive
    #: time, or by OBSERVE if the instruction arrived out-of-band).
    received_event_id: EventId | None = None
    #: Event that recorded the applied effect. Task priority/cancel events
    #: cite this as their ``causation_id``.
    applied_event_id: EventId | None = None
    applied_at: datetime | None = None
    run_id: RunId | None = None
    #: Every event about this intent shares ``correlation_id``.
    correlation_id: str
    outcome: str | None = None
    affected_task_ids: list[TaskId] = Field(default_factory=list)
    reason: str | None = None

    @property
    def intent_id(self) -> OperatorIntentId:
        return self.intent.intent_id


class IntentClassification(BaseModel):
    """Deterministic interpretation of one free-text instruction."""

    intent_type: OperatorIntentType
    redacted_instruction: str
    areas: list[str] = Field(default_factory=list)
    #: Exact, validated endpoint selectors named by the operator (a path or
    #: absolute URL). They can only ever *name already-known* work: the
    #: service matches them against existing Tasks and Attack Graph nodes
    #: and never creates a target. See ``interpreter.extract_endpoint_selectors``.
    endpoint_selectors: list[str] = Field(default_factory=list)
    #: Exact task selectors named with the bounded ``task:<id>`` syntax.
    task_selectors: list[str] = Field(default_factory=list)
    #: What the operator is asking *about* ("candidate", "action",
    #: "blocked", "endpoint", ...) — drives a structured explanation.
    subjects: list[str] = Field(default_factory=list)
    matched_keywords: list[str] = Field(default_factory=list)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    note: str | None = None


class IntentApplication(BaseModel):
    """The result of receiving or applying an intent."""

    status: IntentStatus
    applied: bool
    outcome: str
    intent_id: OperatorIntentId | None = None
    intent_type: OperatorIntentType | None = None
    affected_task_ids: list[TaskId] = Field(default_factory=list)
    reason: str | None = None
    received_event_id: EventId | None = None
    applied_event_id: EventId | None = None


class IntentDrainResult(BaseModel):
    """What OBSERVE learned after draining pending intents for a mission."""

    last_event_id: EventId | None = None
    applications: list[IntentApplication] = Field(default_factory=list)
    last_intent_id: OperatorIntentId | None = None
    paused: bool = False
    stopped: bool = False
    mission_status: MissionStatus | None = None

    @property
    def applied_count(self) -> int:
        return sum(1 for item in self.applications if item.applied)


class IntentStatusEntry(BaseModel):
    """One row of the read-only intent history projection."""

    intent_id: OperatorIntentId
    intent_type: OperatorIntentType
    status: IntentStatus
    created_at: datetime
    outcome: str | None = None


class BlockedTaskExplanation(BaseModel):
    """Why one Task is blocked, with the dependency edge that still holds."""

    task_id: TaskId
    title: str
    blocking_reason: str
    blocked_dependency_ids: list[TaskId] = Field(default_factory=list)


class CandidateExplanation(BaseModel):
    """Why one candidate was dropped (from a ``CandidateRejected`` event).

    ``priority`` is the candidate's scheduler score when the Work Ledger
    supplies it; event-only rejections leave it ``None`` because the
    ``CandidateRejected`` payload does not carry a score. ``evidence_event_id``
    is a reference to the recorded event, never its raw text.
    """

    operation: str
    reason: str | None = None
    field: str | None = None
    candidate_key: str | None = None
    priority: float | None = None
    evidence_event_id: EventId | None = None


class ActionExplanation(BaseModel):
    """One proposed action, joined with its policy outcome by ``action_id``.

    The proposal contributes the operation/target and the scheduler
    ``priority`` (the candidate score); the ``ActionApproved``/``ActionDenied``
    (and ``PolicyEvaluated``) events contribute the verdict and policy reason.
    Everything here is a bounded reference and a redacted reason -- never
    instruction text, arguments, or raw secrets.
    """

    operation: str
    candidate_key: str | None = None
    task_id: TaskId | None = None
    target: str | None = None
    status: str | None = None
    #: Correlates the proposal with its policy outcome (same ``action_id``).
    action_id: str | None = None
    work_id: str | None = None
    #: Scheduler candidate score and its bounded components.
    priority: float | None = None
    priority_components: dict[str, float] = Field(default_factory=dict)
    #: Policy verdict ("allow" / "deny" / "require_approval") when evaluated.
    policy_verdict: str | None = None
    #: Redacted, bounded policy reasons and matched rule ids.
    policy_reasons: list[str] = Field(default_factory=list)
    matched_rules: list[str] = Field(default_factory=list)
    #: Reference to the recording event -- evidence, not raw text.
    evidence_event_id: EventId | None = None


class IntentStatusReport(BaseModel):
    """Read-only status of a Mission's operator-intent channel."""

    mission_id: MissionId
    mission_status: MissionStatus
    active_task_id: TaskId | None = None
    task_count: int = 0
    task_status_counts: dict[str, int] = Field(default_factory=dict)
    paused: bool = False
    focus_areas: list[str] = Field(default_factory=list)
    excluded_areas: list[str] = Field(default_factory=list)
    excluded_endpoints: list[str] = Field(default_factory=list)
    blocked_tasks: list[BlockedTaskExplanation] = Field(default_factory=list)
    recent_intents: list[IntentStatusEntry] = Field(default_factory=list)
    summary: str = ""


class IntentExplanation(BaseModel):
    """Read-only explanation of why the mission is where it is.

    ``summary`` stays a single human line; the structured fields below carry
    the evidence behind it (a specific endpoint, a blocked Task's reason, a
    dropped candidate, the chosen action) so a caller does not have to parse
    prose to answer "why /api/docs? / why is this blocked?".
    """

    mission_id: MissionId
    intent_id: OperatorIntentId | None = None
    summary: str
    mission_status: MissionStatus
    active_task_id: TaskId | None = None
    active_task_title: str | None = None
    paused: bool = False
    task_priorities: dict[str, int] = Field(default_factory=dict)
    #: What the operator asked about ("endpoint", "task", "blocked", ...).
    subjects: list[str] = Field(default_factory=list)
    endpoint_selectors: list[str] = Field(default_factory=list)
    endpoint_matches: list[str] = Field(default_factory=list)
    excluded_areas: list[str] = Field(default_factory=list)
    excluded_endpoints: list[str] = Field(default_factory=list)
    suppressed_selectors: list[str] = Field(default_factory=list)
    blocked_tasks: list[BlockedTaskExplanation] = Field(default_factory=list)
    rejected_candidates: list[CandidateExplanation] = Field(default_factory=list)
    chosen_actions: list[ActionExplanation] = Field(default_factory=list)
    recent_intents: list[IntentStatusEntry] = Field(default_factory=list)


class IntentInterpretationError(ValueError):
    """Raised when an instruction cannot be interpreted deterministically."""
