import pytest

from whs_redteam.config import EngagementConfig
from whs_redteam.exceptions import CapabilityPolicyViolation
from whs_redteam.models import ActionProposal, Capability, RiskLevel
from whs_redteam.policy.capabilities import CapabilityPolicy
from whs_redteam.policy.scope import ScopeGuard


def policy(**updates: object) -> CapabilityPolicy:
    config = EngagementConfig(
        name="capability-test", targets=("https://example.test",), **updates
    )
    return CapabilityPolicy(config, ScopeGuard(config))


def test_passive_discovery_is_approved() -> None:
    proposal = ActionProposal(
        capability=Capability.DISCOVERY,
        target_url="https://example.test/catalog",
        reason="inventory",
        risk_level=RiskLevel.PASSIVE,
    )
    assert policy().approve(proposal).method == "GET"


def test_disabled_capability_is_rejected() -> None:
    proposal = ActionProposal(
        capability=Capability.SQLI_SAFE,
        target_url="https://example.test/search",
        reason="bounded differential probe",
        approval_id="ticket-1",
    )
    with pytest.raises(CapabilityPolicyViolation, match="not enabled"):
        policy().approve(proposal)


def test_sensitive_capability_requires_approval() -> None:
    proposal = ActionProposal(
        capability=Capability.XSS_SAFE,
        target_url="https://example.test/search",
        reason="non-persistent browser canary",
    )
    with pytest.raises(CapabilityPolicyViolation, match="verified approval"):
        policy(enabled_capabilities=frozenset({"xss_safe"})).approve(proposal)


def test_mandatory_approval_cannot_be_disabled_by_config() -> None:
    proposal = ActionProposal(
        capability=Capability.IDOR_SAFE,
        target_url="https://example.test/orders/1",
        reason="role comparison",
    )
    with pytest.raises(CapabilityPolicyViolation, match="verified approval"):
        policy(
            enabled_capabilities=frozenset({"idor_safe"}),
            approval_required_capabilities=frozenset(),
        ).approve(proposal)


def test_fabricated_approval_id_is_not_trusted() -> None:
    proposal = ActionProposal(
        capability=Capability.IDOR_SAFE,
        target_url="https://example.test/orders/1",
        reason="forged planner approval",
        approval_id="forged-approval",
    )
    with pytest.raises(CapabilityPolicyViolation, match="verified approval"):
        policy(enabled_capabilities=frozenset({"idor_safe"})).approve(proposal)


def test_destructive_action_is_always_rejected() -> None:
    proposal = ActionProposal(
        capability=Capability.POC_SAFE,
        target_url="https://example.test/test",
        reason="must be rejected",
        approval_id="ticket-2",
        destructive=True,
    )
    with pytest.raises(CapabilityPolicyViolation, match="never supported"):
        policy(enabled_capabilities=frozenset({"poc_safe"})).approve(proposal)


def test_state_change_requires_engagement_opt_in() -> None:
    proposal = ActionProposal(
        capability=Capability.FILE_UPLOAD_BENIGN,
        target_url="https://example.test/upload",
        method="POST",
        reason="benign marker upload",
        approval_id="ticket-3",
        risk_level=RiskLevel.STATE_CHANGING,
    )
    with pytest.raises(CapabilityPolicyViolation, match="method is not enabled"):
        policy(enabled_capabilities=frozenset({"file_upload_benign"})).approve(proposal)


def test_planner_cannot_downgrade_catalog_risk() -> None:
    proposal = ActionProposal(
        capability=Capability.CSRF_SAFE,
        target_url="https://example.test/profile",
        method="POST",
        reason="must use catalog minimum",
        approval_id="ticket-456",
        risk_level=RiskLevel.PASSIVE,
    )
    with pytest.raises(CapabilityPolicyViolation, match="below capability minimum"):
        policy(
            enabled_capabilities=frozenset({"csrf_safe"}),
            allowed_methods=frozenset({"GET", "POST"}),
            allow_state_change=True,
        ).approve(proposal)


def test_catalog_rejects_method_even_when_engagement_allows_it() -> None:
    proposal = ActionProposal(
        capability=Capability.DISCOVERY,
        target_url="https://example.test/catalog",
        method="POST",
        reason="discovery is read-only",
        risk_level=RiskLevel.PASSIVE,
    )
    with pytest.raises(CapabilityPolicyViolation, match="not valid for capability"):
        policy(
            allowed_methods=frozenset({"GET", "POST"}),
            allow_state_change=True,
        ).approve(proposal)
