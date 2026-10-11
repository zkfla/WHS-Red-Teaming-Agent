"""Pinned destinations reuse the same scope owner, including alias denials."""

from redteam.core.contracts.engagement import EngagementScope
from redteam.core.contracts.target import EndpointTarget
from redteam.core.scope_rules import check_target_scope


def test_registry_name_allow_does_not_erase_pinned_literal_deny():
    scope = EngagementScope(allowed_domains=["example.test"], denied_hosts=["192.0.2.8:443"])
    assert check_target_scope(EndpointTarget(host="api.example.test", port=443), scope)[0]
    assert not check_target_scope(
        EndpointTarget(host="192.0.2.8", port=443), scope, denials_only=True,
    )[0]
    # A denial-only pass grants no authority to an otherwise unknown IP.
    assert check_target_scope(
        EndpointTarget(host="192.0.2.9", port=443), scope, denials_only=True,
    )[0]
    assert not check_target_scope(EndpointTarget(host="192.0.2.9", port=443), scope)[0]


def test_registry_port_and_malformed_denial_use_existing_scope_contract():
    scope = EngagementScope(allowed_hosts=["api.example.test:443"])
    assert check_target_scope(EndpointTarget(host="api.example.test", port=443), scope)[0]
    assert not check_target_scope(EndpointTarget(host="api.example.test", port=80), scope)[0]
    assert not check_target_scope(EndpointTarget(host="api.example.test"), scope)[0]
    broken = scope.model_copy(update={"denied_hosts": ["bad:port"]})
    assert not check_target_scope(
        EndpointTarget(host="192.0.2.9", port=443), broken, denials_only=True,
    )[0]
