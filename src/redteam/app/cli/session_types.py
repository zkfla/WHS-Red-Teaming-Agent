"""CLI-owned identities and receipts; never authoritative runtime state."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class SessionPhase(str, Enum):
    IDLE = "idle"
    STARTING = "starting"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    PAUSED = "paused"
    SETTLING = "settling"
    CLOSING = "closing"
    CLOSED = "closed"


@dataclass(frozen=True)
class BindingRef:
    start_request_id: str
    attachment_id: str
    run_id: str | None = None


@dataclass(frozen=True)
class InputEnvelope:
    input_id: str
    text: str
    received_sequence: int
    submitted_phase: SessionPhase
    observed_binding: BindingRef | None
    draft_binding: BindingRef | None
    has_draft_binding: bool = False
    approval_id: str | None = None
    received_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def binding(self) -> BindingRef | None:
        return self.draft_binding if self.has_draft_binding else self.observed_binding


@dataclass
class InputReceipt:
    envelope: InputEnvelope
    state: str = "queued"
    reason: str = ""
    decision: Any = None
    context_revision: Any = None


@dataclass
class RunResult:
    final: Any = None
    exit_code: int = 0
    error: str | None = None


@dataclass(frozen=True)
class RunCompletionView:
    binding: BindingRef
    phase: str | None
    reason: str | None
    mission_status: str | None
    outcome: str
    exit_code: int
    actions: int = 0
    observations: tuple[str, ...] = ()
    error: str | None = None

    @classmethod
    def from_result(cls, binding: BindingRef, result: RunResult) -> RunCompletionView:
        state = result.final
        phase = getattr(getattr(state, "phase", None), "value", None)
        reason = getattr(state, "termination_reason", None)
        mission = getattr(getattr(state, "mission_status", None), "value", None)
        outcome = "ended"
        if result.error or result.exit_code or phase == "failed":
            outcome = "failed"
        elif reason in {"approval_pending", "operator_paused"}:
            outcome = "suspended"
        elif state is None:
            outcome = "unknown"
        return cls(
            binding,
            phase,
            reason,
            mission,
            outcome,
            result.exit_code,
            getattr(state, "actions_executed", 0),
            tuple(item.summary for item in getattr(state, "recent_observations", [])[-5:]),
            result.error,
        )
