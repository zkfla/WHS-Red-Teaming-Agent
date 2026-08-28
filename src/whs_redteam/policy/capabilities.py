"""Fail-closed authorization for proposed agent capabilities."""

from __future__ import annotations

from whs_redteam.config import (
    MANDATORY_APPROVAL_CAPABILITIES,
    SAFE_METHODS,
    EngagementConfig,
)
from whs_redteam.exceptions import CapabilityPolicyViolation
from whs_redteam.models import ActionProposal, RiskLevel
from whs_redteam.policy.capability_catalog import CAPABILITY_CATALOG
from whs_redteam.policy.scope import ScopeGuard


class CapabilityPolicy:
    """Approve structured proposals without granting a planner direct tool access."""

    def __init__(self, config: EngagementConfig, scope_guard: ScopeGuard) -> None:
        self.config = config
        self.scope_guard = scope_guard

    def approve(
        self, proposal: ActionProposal, *, approval_verified: bool = False
    ) -> ActionProposal:
        capability = proposal.capability.value
        method = proposal.method.upper()
        spec = CAPABILITY_CATALOG[proposal.capability]
        if capability not in self.config.enabled_capabilities:
            raise CapabilityPolicyViolation(f"capability is not enabled: {capability}")
        if proposal.destructive:
            raise CapabilityPolicyViolation("destructive actions are never supported")
        if proposal.actor_id and proposal.actor_id not in {
            actor.actor_id for actor in self.config.actors
        }:
            raise CapabilityPolicyViolation(f"unknown actor: {proposal.actor_id}")
        if method not in self.config.allowed_methods:
            raise CapabilityPolicyViolation(f"method is not enabled: {method}")
        if method not in spec.allowed_methods:
            raise CapabilityPolicyViolation(
                f"method is not valid for capability {capability}: {method}"
            )

        risk_order = {
            RiskLevel.PASSIVE: 0,
            RiskLevel.SAFE_ACTIVE: 1,
            RiskLevel.STATE_CHANGING: 2,
        }
        if risk_order[proposal.risk_level] < risk_order[spec.minimum_risk]:
            raise CapabilityPolicyViolation(
                f"risk level is below capability minimum: {spec.minimum_risk.value}"
            )

        state_changing = method not in SAFE_METHODS or (
            proposal.risk_level is RiskLevel.STATE_CHANGING
        )
        if state_changing and not self.config.allow_state_change:
            raise CapabilityPolicyViolation("state-changing action is disabled")
        approval_required = (
            self.config.approval_required_capabilities | MANDATORY_APPROVAL_CAPABILITIES
        )
        if (spec.approval_required or capability in approval_required) and not approval_verified:
            raise CapabilityPolicyViolation(
                f"verified approval is required for capability: {capability}"
            )

        self.scope_guard.authorize(proposal.target_url, method)
        return proposal.model_copy(update={"method": method})
