"""Scope Deny Test: out-of-scope/denied targets never get ALLOW."""

from __future__ import annotations

from datetime import datetime, timezone

from redteam.control.scope import check_scope
from redteam.core.contracts.action import ActionProposal
from redteam.core.contracts.engagement import EngagementScope
from redteam.core.contracts.target import (
    AnyTarget,
    DomainTarget,
    HostTarget,
    PrincipalTarget,
    UrlTarget,
)
from redteam.core.ids import ActionId, AgentId, EngagementId, PrincipalId, RunId


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
    ok, reasons = check_scope(_proposal("good.local"), scope)
    assert ok is True
    assert reasons == []


def test_host_outside_allow_list_is_denied() -> None:
    scope = EngagementScope(allowed_hosts=["good.local"])
    ok, reasons = check_scope(_proposal("unknown.local"), scope)
    assert ok is False
    assert reasons


def test_explicitly_denied_host_is_denied_even_if_allowed() -> None:
    scope = EngagementScope(allowed_hosts=["bad.local"], denied_hosts=["bad.local"])
    ok, reasons = check_scope(_proposal("bad.local"), scope)
    assert ok is False
    assert reasons


def test_empty_scope_defaults_to_deny() -> None:
    scope = EngagementScope()
    ok, reasons = check_scope(_proposal("anything"), scope)
    assert ok is False
    assert reasons


# --- CIDR / IP ---------------------------------------------------------


def test_ip_in_allowed_network_passes() -> None:
    scope = EngagementScope(allowed_networks=["10.0.0.0/8"])
    assert check_scope(_proposal("10.1.2.3"), scope)[0] is True


def test_ip_outside_allowed_network_is_denied() -> None:
    scope = EngagementScope(allowed_networks=["10.0.0.0/8"])
    ok, _ = check_scope(_proposal("11.0.0.1"), scope)
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
    ok, _ = check_scope(_proposal("notexample.com"), scope)
    assert ok is False


def test_denied_domain_suffix_wins() -> None:
    scope = EngagementScope(
        allowed_domains=["example.com"],
        denied_domains=["internal.example.com"],
    )
    ok, _ = check_scope(_proposal("db.internal.example.com"), scope)
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
    ok, reasons = check_scope(_proposal_for(target), scope)
    assert ok is False
    assert "no scope-checkable" in reasons[0]
