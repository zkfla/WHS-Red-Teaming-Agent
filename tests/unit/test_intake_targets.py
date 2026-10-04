"""Explicit operator addresses retain the shared scope's port semantics."""

import pytest

from redteam.core.contracts.engagement import EngagementScope
from redteam.core.contracts.target import UrlTarget
from redteam.core.scope_rules import target_matches_selector
from redteam.intake.targets import explicit_target_origins


@pytest.mark.parametrize(
    ("description", "origins"),
    [
        ("http://localhost:8081에서 mock-bank 조사", {"localhost:8081": "http://localhost:8081"}),
        ("https://example.test/api를 조사", {"example.test:443": "https://example.test:443"}),
        ("http://[::1]:8081/ 조사", {"[::1]:8081": "http://[::1]:8081"}),
        ("example.test:8443 조사", {"example.test:8443": "http://example.test:8443"}),
        ("목뱅크 취약점 조사", {}),
        ("http://localhost:bad/ 조사", {}),
        ("http://localhost:99999/ 조사", {}),
        ("http://user:password@localhost:8081/ 조사", {}),
    ],
)
def test_only_explicit_addresses_become_scope(description, origins):
    assert explicit_target_origins(description) == origins


def test_url_scope_does_not_authorize_other_ports_or_subdomains():
    hosts = list(explicit_target_origins("https://example.test/path"))
    scope = EngagementScope(allowed_hosts=hosts)
    assert target_matches_selector(scope, UrlTarget(url="https://example.test/other"))
    assert not target_matches_selector(scope, UrlTarget(url="http://example.test/"))
    assert not target_matches_selector(scope, UrlTarget(url="https://sub.example.test/"))


def test_explicit_address_list_accepts_bare_hosts_and_deduplicates_urls():
    assert explicit_target_origins(
        "bank-internal, http://localhost:8081/a, http://localhost:8081/b", address_list=True
    ) == {"bank-internal": "http://bank-internal", "localhost:8081": "http://localhost:8081"}
    with pytest.raises(ValueError):
        explicit_target_origins("localhost:8081, http://localhost:bad/", address_list=True)
