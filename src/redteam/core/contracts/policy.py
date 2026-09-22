"""Control Plane input context, decision output, and approval record.

Owned by PM (schema) — Control Team owns the evaluation logic that
produces these in ``redteam.control``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from redteam.core.enums import ApprovalStatus, PolicyVerdict
from redteam.core.ids import ActionId, AgentId, ApprovalId, EngagementId


class ControlContext(BaseModel):
    """Everything ``ControlPlane.evaluate`` needs beyond the proposal itself."""

    engagement_id: EngagementId
    agent_id: AgentId | None = None
    correlation_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class PolicyDecision(BaseModel):
    action_id: ActionId
    verdict: PolicyVerdict
    reasons: list[str] = Field(default_factory=list)

    matched_rules: list[str] = Field(default_factory=list)

    approval_id: ApprovalId | None = None

    rate_limit_remaining: int | None = None
    budget_remaining: int | None = None

    evaluated_at: datetime


class Approval(BaseModel):
    approval_id: ApprovalId
    action_id: ActionId
    status: ApprovalStatus
    requested_at: datetime
    resolved_at: datetime | None = None
    resolved_by: str | None = None
    notes: str = ""
