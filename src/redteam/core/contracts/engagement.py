"""Engagement scope, rules of engagement, and execution limits.

Owned by PM (schema) — Control Team implements the enforcement logic
against these shapes in ``redteam.control``. Policy Engine logic itself
is intentionally not implemented here — schema stability matters more
than completeness at this stage.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from redteam.core.enums import RiskLevel
from redteam.core.ids import ApprovalId, EngagementId


class EngagementScope(BaseModel):
    allowed_hosts: list[str] = Field(default_factory=list)
    allowed_domains: list[str] = Field(default_factory=list)
    allowed_networks: list[str] = Field(default_factory=list)

    denied_hosts: list[str] = Field(default_factory=list)
    denied_domains: list[str] = Field(default_factory=list)

    # Mobile artifacts (`AssetTarget.asset_id`) have no host identity, so
    # they cannot match allowed_hosts/domains/networks above — they are
    # scoped separately, by the same canonical identifier the rest of the
    # framework already uses for an asset (`core.contracts.target.AssetTarget.asset_id`,
    # e.g. an Android package id or an operator-assigned APK/IPA
    # reference). Exact match only — see `redteam.control.scope`.
    allowed_assets: list[str] = Field(default_factory=list)
    denied_assets: list[str] = Field(default_factory=list)


class RuleOfEngagement(BaseModel):
    name: str
    description: str = ""

    allowed_capabilities: list[str] = Field(default_factory=list)
    denied_capabilities: list[str] = Field(default_factory=list)
    allowed_operations: list[str] = Field(default_factory=list)
    denied_operations: list[str] = Field(default_factory=list)

    max_parallel_actions: int | None = Field(default=None, ge=1)
    availability_impact_allowed: bool = False

    start_at: datetime | None = None
    end_at: datetime | None = None

    allowed_time_windows: list[str] = Field(default_factory=list)
    prohibited_techniques: list[str] = Field(default_factory=list)
    notes: str = ""


class ExecutionBudget(BaseModel):
    max_actions: int = Field(ge=0)
    max_tool_calls: int | None = Field(default=None, ge=0)
    max_duration_seconds: int | None = Field(default=None, ge=0)


class RateLimitPolicy(BaseModel):
    max_actions_per_minute: int = Field(ge=0)
    max_concurrent_actions: int = Field(default=1, ge=1)


class ApprovalPolicy(BaseModel):
    require_approval_for: list[str] = Field(default_factory=list)
    default_approval_id: ApprovalId | None = None
    #: Legacy YAML compatibility: ``True`` allows auto-approval of *known LOW*
    #: risk only. HIGH/CRITICAL and unknown risk always require a human.
    auto_approve: bool = False

    #: Risk level at or above which an otherwise-permitted action needs
    #: human approval. ``None`` falls back to the Engine default
    #: (``control.risk.DEFAULT_APPROVAL_RISK_THRESHOLD``), preserving the
    #: pre-configurable behaviour.
    risk_threshold: RiskLevel | None = None

    #: Bounded auto-approval: auto-approve an action whose resolved risk is
    #: at or below this level, and require approval above it. ``None`` means
    #: no bounded auto-approval. When set, it takes precedence over the
    #: legacy ``auto_approve`` flag. Unknown (unresolvable) risk is never
    #: auto-approved under a bounded ceiling, since it may exceed it.
    auto_approve_up_to: RiskLevel | None = None


class Engagement(BaseModel):
    engagement_id: EngagementId
    name: str
    description: str = ""

    scope: EngagementScope
    rules_of_engagement: list[RuleOfEngagement] = Field(default_factory=list)
    execution_budget: ExecutionBudget
    rate_limit_policy: RateLimitPolicy
    approval_policy: ApprovalPolicy

    created_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)
