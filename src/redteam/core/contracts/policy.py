"""Control Plane input context, decision output, and approval record.

Owned by PM (schema) — Control Team owns the evaluation logic that
produces these in ``redteam.control``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from redteam.core.enums import ApprovalStatus, PolicyVerdict, Severity
from redteam.core.ids import ActionId, AgentId, ApprovalId, EngagementId, ToolId


class ControlContext(BaseModel):
    """Everything ``ControlPlane.evaluate`` needs beyond the proposal itself."""

    engagement_id: EngagementId
    agent_id: AgentId | None = None
    correlation_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    in_flight_actions: int | None = Field(default=None, ge=0)
    # Assigned by the execution path from the registered, compiled tool binding.
    selected_tool_id: ToolId | None = None


class PolicyDecision(BaseModel):
    action_id: ActionId
    verdict: PolicyVerdict
    reasons: list[str] = Field(default_factory=list)

    matched_rules: list[str] = Field(default_factory=list)

    approval_id: ApprovalId | None = None

    rate_limit_remaining: int | None = None
    budget_remaining: int | None = None

    evaluated_at: datetime
    severity: Severity | None = None
    human_required: bool = False
    execution_fingerprint: str | None = None

    @property
    def denial_severity(self) -> str | None:
        """Typed new decisions and the original tag meaning for old records."""
        if self.verdict is not PolicyVerdict.DENY:
            return None
        if self.severity is not None:
            return self.severity.value
        return legacy_denial_severity(self.matched_rules)


def legacy_denial_severity(matched_rules: list[str]) -> str | None:
    for rule in matched_rules:
        if rule.startswith("severity:"):
            return rule[len("severity:") :] or None
    return None


class Approval(BaseModel):
    approval_id: ApprovalId
    action_id: ActionId
    status: ApprovalStatus
    requested_at: datetime
    resolved_at: datetime | None = None
    resolved_by: str | None = None
    notes: str = ""
    execution_fingerprint: str | None = None
