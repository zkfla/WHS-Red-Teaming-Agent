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
    DomainTarget,
    EndpointTarget,
    HostTarget,
    PrincipalTarget,
    ServiceTarget,
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


# --- port-aware host:port scoping --------------------------------------
#
# Regression for the Control-team report (2026-10-01): the same target,
# expressed differently, used to flip ALLOW/DENY, and a port-suffixed host
# could slip past a bare-host deny entry. Scope entries carry an optional
# ``host:port`` convention inside the existing ``list[str]`` fields.


def test_host_with_port_matches_same_host_port_entry() -> None:
    # Report row 1 (was: pass) — stays a pass.
    scope = EngagementScope(allowed_hosts=["127.0.0.1:8080"])
    assert check_scope(_proposal("127.0.0.1:8080"), scope)[0] is True


def test_url_with_port_matches_host_port_entry() -> None:
    # Report row 2 (was: wrongly DENIED) — URL port must be honoured.
    scope = EngagementScope(allowed_hosts=["127.0.0.1:8080"])
    target = UrlTarget(url="http://127.0.0.1:8080/")
    assert check_scope(_proposal_for(target), scope)[0] is True


def test_endpoint_port_field_matches_host_port_entry() -> None:
    # Report row 3 (was: wrongly DENIED) — EndpointTarget.port must count.
    scope = EngagementScope(allowed_hosts=["127.0.0.1:8080"])
    target = EndpointTarget(host="127.0.0.1", port=8080)
    assert check_scope(_proposal_for(target), scope)[0] is True


def test_bare_host_allow_accepts_any_port() -> None:
    # Report row 4 (was: wrongly DENIED) — a bare host allows every port.
    scope = EngagementScope(allowed_hosts=["127.0.0.1"])
    assert check_scope(_proposal("127.0.0.1:8080"), scope)[0] is True
    endpoint = EndpointTarget(host="127.0.0.1", port=9090)
    assert check_scope(_proposal_for(endpoint), scope)[0] is True


def test_port_suffix_cannot_bypass_bare_host_deny() -> None:
    # Report row 5 (was: wrongly PASSED) — a bare-host deny blocks all
    # ports; adding :8080 must not dodge the deny rule.
    scope = EngagementScope(
        allowed_hosts=["127.0.0.1:8080"], denied_hosts=["127.0.0.1"]
    )
    ok, reasons, severity = check_scope(_proposal("127.0.0.1:8080"), scope)
    assert ok is False
    assert "denied_hosts" in reasons[0]
    assert severity is Severity.BLOCK_ACTION


def test_wrong_port_is_denied_when_entry_pins_port() -> None:
    scope = EngagementScope(allowed_hosts=["127.0.0.1:8080"])
    assert check_scope(_proposal("127.0.0.1:9090"), scope)[0] is False


def test_port_pinned_deny_blocks_only_that_port() -> None:
    scope = EngagementScope(
        allowed_hosts=["127.0.0.1"], denied_hosts=["127.0.0.1:22"]
    )
    # The pinned deny port is blocked...
    assert check_scope(_proposal("127.0.0.1:22"), scope)[0] is False
    # ...but other ports on the bare-allowed host still pass.
    assert check_scope(_proposal("127.0.0.1:8080"), scope)[0] is True


def test_allow_pins_port_but_target_has_none_is_explicit_deny() -> None:
    # Control must not infer the port: host matches, port unknown -> DENY
    # with a port-specific reason (not a silent default-deny).
    scope = EngagementScope(allowed_hosts=["127.0.0.1:8080"])
    ok, reasons, severity = check_scope(_proposal("127.0.0.1"), scope)
    assert ok is False
    assert "port cannot be inferred" in reasons[0]
    assert severity is Severity.BLOCK_ACTION


def test_endpoint_port_conflict_is_denied() -> None:
    # host string and the explicit port field disagree -> reasoned DENY.
    scope = EngagementScope(allowed_hosts=["127.0.0.1"])
    target = EndpointTarget(host="127.0.0.1:8080", port=9090)
    ok, reasons, _ = check_scope(_proposal_for(target), scope)
    assert ok is False
    assert "conflict" in reasons[0]


def test_endpoint_host_string_port_is_used_when_field_absent() -> None:
    scope = EngagementScope(allowed_hosts=["127.0.0.1:8080"])
    target = EndpointTarget(host="127.0.0.1:8080")
    assert check_scope(_proposal_for(target), scope)[0] is True


def test_malformed_port_is_reasoned_deny() -> None:
    scope = EngagementScope(allowed_hosts=["127.0.0.1"])
    ok, reasons, severity = check_scope(_proposal("127.0.0.1:notaport"), scope)
    assert ok is False
    assert "malformed" in reasons[0]
    assert severity is Severity.BLOCK_ACTION


def test_out_of_range_endpoint_port_is_reasoned_deny() -> None:
    scope = EngagementScope(allowed_hosts=["127.0.0.1"])
    target = EndpointTarget(host="127.0.0.1", port=99999)
    ok, reasons, _ = check_scope(_proposal_for(target), scope)
    assert ok is False
    assert "invalid endpoint port" in reasons[0]


# --- IPv6 is not corrupted by naive colon-splitting --------------------


def test_bracketed_ipv6_with_port_matches() -> None:
    scope = EngagementScope(allowed_hosts=["[::1]:8080"])
    target = UrlTarget(url="http://[::1]:8080/")
    assert check_scope(_proposal_for(target), scope)[0] is True


def test_bare_ipv6_literal_allowed_on_any_port() -> None:
    scope = EngagementScope(allowed_hosts=["::1"])
    assert check_scope(_proposal("::1"), scope)[0] is True
    # Same address, bracketed+port form, must still match the bare allow.
    assert check_scope(_proposal_for(UrlTarget(url="http://[::1]:8080/")), scope)[0] is True


def test_bare_ipv6_deny_blocks_ported_request() -> None:
    scope = EngagementScope(allowed_hosts=["[::1]:8080"], denied_hosts=["::1"])
    assert check_scope(_proposal_for(UrlTarget(url="http://[::1]:8080/")), scope)[0] is False


# --- service target without host still denies --------------------------


def test_service_target_without_host_is_denied() -> None:
    scope = EngagementScope(allowed_hosts=["127.0.0.1"])
    target = ServiceTarget(service_name="smtp")
    ok, reasons, _ = check_scope(_proposal_for(target), scope)
    assert ok is False
    assert "no scope-checkable" in reasons[0]


# --- fail-safe hardening (teammate review, 2026-10-02) ---------------------
#
# Three fail-open holes in the first port-aware cut, fixed to fail closed:
#   1. an empty port ("host:") was read as a bare host = all ports
#   2. a malformed deny entry was silently skipped (deny rule with no effect)
#   3. a port-scoped deny was bypassed by a scheme-only URL / a portless target


def test_empty_port_entry_is_rejected_not_all_ports() -> None:
    # "127.0.0.1:" must NOT be read as a bare host (which would allow every
    # port); it is a malformed allow entry and grants nothing.
    scope = EngagementScope(allowed_hosts=["127.0.0.1:"])
    ok, _, _ = check_scope(_proposal("127.0.0.1:9999"), scope)
    assert ok is False


def test_empty_port_target_is_reasoned_deny() -> None:
    scope = EngagementScope(allowed_hosts=["127.0.0.1"])
    ok, reasons, severity = check_scope(_proposal("127.0.0.1:"), scope)
    assert ok is False
    assert "malformed" in reasons[0]
    assert severity is Severity.BLOCK_ACTION


def test_malformed_deny_entry_fails_closed() -> None:
    # A broken deny rule must not silently give zero protection: refuse until
    # the config is fixed, rather than letting the target through.
    scope = EngagementScope(allowed_hosts=["10.0.0.5"], denied_hosts=["10.0.0.5:abc"])
    ok, reasons, severity = check_scope(_proposal("10.0.0.5:8080"), scope)
    assert ok is False
    assert "malformed denied_hosts" in reasons[0]
    assert severity is Severity.BLOCK_ACTION


def test_valid_deny_still_works_alongside_check() -> None:
    # Sanity: with only well-formed deny entries nothing fails closed spuriously.
    scope = EngagementScope(allowed_hosts=["10.0.0.5"], denied_hosts=["10.0.0.6"])
    assert check_scope(_proposal("10.0.0.5:8080"), scope)[0] is True


def test_scheme_only_url_infers_default_port() -> None:
    # http:// -> 80, so a host:80 rule applies to a URL with no explicit port.
    scope = EngagementScope(allowed_hosts=["example.com:80"])
    assert check_scope(_proposal_for(UrlTarget(url="http://example.com/")), scope)[0] is True
    https = EngagementScope(allowed_hosts=["example.com:443"])
    assert check_scope(_proposal_for(UrlTarget(url="https://example.com/")), https)[0] is True


def test_port_scoped_deny_not_bypassed_by_scheme_only_url() -> None:
    scope = EngagementScope(allowed_hosts=["example.com"], denied_hosts=["example.com:80"])
    ok, reasons, _ = check_scope(_proposal_for(UrlTarget(url="http://example.com/")), scope)
    assert ok is False
    assert "denied_hosts" in reasons[0]


def test_portless_target_under_port_scoped_deny_fails_closed() -> None:
    # Host matches a port-scoped deny but carries no port: we cannot prove it
    # is a *different* port, so refuse.
    scope = EngagementScope(allowed_hosts=["10.0.0.5"], denied_hosts=["10.0.0.5:22"])
    ok, reasons, severity = check_scope(_proposal("10.0.0.5"), scope)
    assert ok is False
    assert "port-scoped deny" in reasons[0]
    assert severity is Severity.BLOCK_ACTION
