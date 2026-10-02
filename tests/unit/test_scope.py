"""Scope Deny Test: out-of-scope/denied targets never get ALLOW.

Covers exact host / IP, CIDR networks, and domain-suffix matching.
``check_scope`` returns ``(allowed, reasons, severity)``; every scope
denial is ``BLOCK_ACTION`` (try a different in-scope target).
"""

from __future__ import annotations

from datetime import datetime, timezone

from redteam.control.scope import check_scope
from redteam.control.severity import Severity
from redteam.core.contracts.action import ActionProposal
from redteam.core.contracts.engagement import EngagementScope
from redteam.core.contracts.target import (
    AnyTarget,
    AssetTarget,
    DeviceTarget,
    DomainTarget,
    HostTarget,
    PrincipalTarget,
    UrlTarget,
)
from redteam.core.ids import ActionId, AgentId, AssetId, EngagementId, PrincipalId, RunId


def _proposal_for(target: AnyTarget) -> ActionProposal:
    return ActionProposal(
        action_id=ActionId("action_1"),
        run_id=RunId("run_1"),
        engagement_id=EngagementId("eng_1"),
        agent_id=AgentId("agent_1"),
        capability="recon",
        operation="port_scan",
        target=target,
        reason="test",
        created_at=datetime.now(timezone.utc),
    )


def _proposal(host: str) -> ActionProposal:
    return _proposal_for(HostTarget(host=host))


def test_allowed_host_passes() -> None:
    scope = EngagementScope(allowed_hosts=["good.local"])
    ok, reasons, severity = check_scope(_proposal("good.local"), scope)
    assert ok is True
    assert reasons == []
    assert severity is None


def test_host_outside_allow_list_is_denied() -> None:
    scope = EngagementScope(allowed_hosts=["good.local"])
    ok, reasons, severity = check_scope(_proposal("unknown.local"), scope)
    assert ok is False
    assert reasons
    assert severity is Severity.BLOCK_ACTION


def test_explicitly_denied_host_is_denied_even_if_allowed() -> None:
    scope = EngagementScope(allowed_hosts=["bad.local"], denied_hosts=["bad.local"])
    ok, reasons, _ = check_scope(_proposal("bad.local"), scope)
    assert ok is False
    assert reasons


def test_empty_scope_defaults_to_deny() -> None:
    scope = EngagementScope()
    ok, reasons, _ = check_scope(_proposal("anything"), scope)
    assert ok is False
    assert reasons


# --- CIDR / IP ---------------------------------------------------------


def test_ip_in_allowed_network_passes() -> None:
    scope = EngagementScope(allowed_networks=["10.0.0.0/8"])
    assert check_scope(_proposal("10.1.2.3"), scope)[0] is True


def test_ip_outside_allowed_network_is_denied() -> None:
    scope = EngagementScope(allowed_networks=["10.0.0.0/8"])
    ok, _, _ = check_scope(_proposal("11.0.0.1"), scope)
    assert ok is False


def test_ip_literal_exact_host_passes() -> None:
    scope = EngagementScope(allowed_hosts=["192.168.0.5"])
    assert check_scope(_proposal("192.168.0.5"), scope)[0] is True


def test_hostname_not_matched_by_network() -> None:
    # No DNS resolution: a hostname never matches a CIDR range.
    scope = EngagementScope(allowed_networks=["10.0.0.0/8"])
    assert check_scope(_proposal("host.local"), scope)[0] is False


# --- domain suffix -----------------------------------------------------


def test_subdomain_matches_allowed_domain() -> None:
    scope = EngagementScope(allowed_domains=["example.com"])
    assert check_scope(_proposal("api.example.com"), scope)[0] is True


def test_exact_domain_matches() -> None:
    scope = EngagementScope(allowed_domains=["example.com"])
    assert check_scope(_proposal("example.com"), scope)[0] is True


def test_lookalike_domain_is_denied() -> None:
    # Bug guard: "notexample.com" must NOT match "example.com"
    # (a naive str.endswith would wrongly allow it).
    scope = EngagementScope(allowed_domains=["example.com"])
    ok, _, _ = check_scope(_proposal("notexample.com"), scope)
    assert ok is False


def test_denied_domain_suffix_wins() -> None:
    scope = EngagementScope(
        allowed_domains=["example.com"],
        denied_domains=["internal.example.com"],
    )
    ok, _, _ = check_scope(_proposal("db.internal.example.com"), scope)
    assert ok is False


# --- other target kinds ------------------------------------------------


def test_url_target_matched_by_domain() -> None:
    scope = EngagementScope(allowed_domains=["example.com"])
    target = UrlTarget(url="https://api.example.com/admin")
    assert check_scope(_proposal_for(target), scope)[0] is True


def test_domain_target_matched() -> None:
    scope = EngagementScope(allowed_domains=["example.com"])
    assert check_scope(_proposal_for(DomainTarget(domain="example.com")), scope)[0] is True


def test_principal_target_has_no_scope_identity() -> None:
    scope = EngagementScope(allowed_hosts=["anything"])
    target = PrincipalTarget(principal_id=PrincipalId("p1"))
    ok, reasons, _ = check_scope(_proposal_for(target), scope)
    assert ok is False
    assert "no scope-checkable" in reasons[0]


# --- mobile assets -------------------------------------------------------


def test_asset_target_allowed_when_listed() -> None:
    scope = EngagementScope(allowed_assets=["com.example.bank"])
    target = AssetTarget(asset_id=AssetId("com.example.bank"))
    ok, reasons, severity = check_scope(_proposal_for(target), scope)
    assert ok is True
    assert reasons == []
    assert severity is None


def test_asset_target_denied_when_not_listed() -> None:
    scope = EngagementScope(allowed_assets=["com.example.bank"])
    target = AssetTarget(asset_id=AssetId("com.example.other"))
    ok, reasons, severity = check_scope(_proposal_for(target), scope)
    assert ok is False
    assert "not within engagement scope" in reasons[0]
    assert severity is Severity.BLOCK_ACTION


def test_asset_target_denied_by_default_with_no_allowlist() -> None:
    """Regression: before allowed_assets/denied_assets existed, an
    AssetTarget had no scope-checkable identity at all and always fell
    through to the generic host-based DENY — Mobile actions were
    unconditionally denied regardless of engagement config."""
    scope = EngagementScope()
    target = AssetTarget(asset_id=AssetId("com.example.bank"))
    ok, reasons, _ = check_scope(_proposal_for(target), scope)
    assert ok is False
    assert "allowed_assets" in reasons[0]


def test_asset_target_denied_list_wins_even_if_allowed() -> None:
    scope = EngagementScope(
        allowed_assets=["com.example.bank"], denied_assets=["com.example.bank"]
    )
    target = AssetTarget(asset_id=AssetId("com.example.bank"))
    ok, reasons, _ = check_scope(_proposal_for(target), scope)
    assert ok is False
    assert "denied_assets" in reasons[0]


def test_asset_target_match_is_case_sensitive_exact_match() -> None:
    """No host-style case-folding or substring/prefix matching for
    canonical asset identifiers."""
    scope = EngagementScope(allowed_assets=["com.example.Bank"])
    target = AssetTarget(asset_id=AssetId("com.example.bank"))
    assert check_scope(_proposal_for(target), scope)[0] is False

    substring_scope = EngagementScope(allowed_assets=["example.bank"])
    assert check_scope(_proposal_for(target), substring_scope)[0] is False


def test_asset_scope_does_not_use_host_allow_lists() -> None:
    """An asset_id that happens to look like a hostname must not be
    granted scope via allowed_hosts/domains/networks — assets and hosts
    are different identity spaces."""
    scope = EngagementScope(allowed_hosts=["com.example.bank"])
    target = AssetTarget(asset_id=AssetId("com.example.bank"))
    assert check_scope(_proposal_for(target), scope)[0] is False


def test_device_target_uses_its_linked_asset_scope() -> None:
    scope = EngagementScope(allowed_assets=["insecurebankv2"])
    target = DeviceTarget(
        device_serial="emulator-5554",
        device_ref="redteam_x86",
        package_name="com.android.insecurebankv2",
        asset_id=AssetId("insecurebankv2"),
    )
    assert check_scope(_proposal_for(target), scope)[0] is True
