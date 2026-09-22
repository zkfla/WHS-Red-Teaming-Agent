"""Scope Check — the first pipeline stage in ``DefaultControlPlane.evaluate``.

Resolves each target to a host identity and matches it against the
engagement's allow/deny lists:

  - ``allowed_hosts`` / ``denied_hosts``   — exact host or IP-literal match
  - ``allowed_domains`` / ``denied_domains`` — domain-suffix match
    (``api.example.com`` is within ``example.com``; ``notexample.com`` is
    NOT — a plain ``str.endswith`` would wrongly allow it)
  - ``allowed_networks``                    — CIDR membership for
    IP-literal targets (``ipaddress``)
  - ``allowed_assets`` / ``denied_assets``  — exact-match on
    ``AssetTarget.asset_id`` (mobile artifacts have no host identity, so
    they cannot use the rules above; see ``_check_asset_scope``)

Deny lists win over allow lists; with no allow-list configured the
default is DENY (safer for a red-team gate).

Notes / limitations:
  - No DNS resolution: ``allowed_networks`` only matches targets that are
    already IP literals, not hostnames that would resolve into the range.
  - ``principal`` / ``graph_node`` targets have no host identity and
    therefore fall through to DENY (scope is host-based). ``asset`` is
    handled separately, by ``allowed_assets``/``denied_assets``.
"""

from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit

from redteam.control.severity import Severity
from redteam.core.contracts.action import ActionProposal
from redteam.core.contracts.engagement import EngagementScope
from redteam.core.contracts.target import (
    AnyTarget,
    AssetTarget,
    DomainTarget,
    EndpointTarget,
    HostTarget,
    ServiceTarget,
    UrlTarget,
)


def _url_host(url: str) -> str | None:
    parts = urlsplit(url if "://" in url else "//" + url)
    return parts.hostname


def _target_hosts(target: AnyTarget) -> list[str]:
    """Host-like identity strings to match against scope, or [] if the
    target kind has none (principal/asset/graph_node)."""
    if isinstance(target, HostTarget):
        return [target.host]
    if isinstance(target, DomainTarget):
        return [target.domain]
    if isinstance(target, UrlTarget):
        host = _url_host(target.url)
        return [host] if host else []
    if isinstance(target, EndpointTarget):
        return [target.host]
    if isinstance(target, ServiceTarget):
        return [target.host] if target.host else []
    return []


def _as_ip(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(value)
    except ValueError:
        return None


def _matches_host(candidate: str, entries: list[str]) -> bool:
    cand_ip = _as_ip(candidate)
    for entry in entries:
        if candidate.lower() == entry.lower():
            return True
        entry_ip = _as_ip(entry)
        if cand_ip is not None and entry_ip is not None and cand_ip == entry_ip:
            return True
    return False


def _matches_domain(candidate: str, domains: list[str]) -> bool:
    if _as_ip(candidate) is not None:
        return False  # an IP literal is never a domain-suffix match
    cand = candidate.lower().rstrip(".")
    for domain in domains:
        base = domain.lower().rstrip(".")
        if cand == base or cand.endswith("." + base):
            return True
    return False


def _matches_network(candidate: str, networks: list[str]) -> bool:
    ip = _as_ip(candidate)
    if ip is None:
        return False
    for cidr in networks:
        try:
            net = ipaddress.ip_network(cidr, strict=False)
        except ValueError:
            continue  # malformed CIDR in config — skip, don't crash the gate
        if ip.version == net.version and ip in net:
            return True
    return False


def _check_asset_scope(
    target: AssetTarget, scope: EngagementScope
) -> tuple[bool, list[str], Severity | None]:
    """Exact-match scope check for mobile artifacts.

    Deliberately not a substring/path check: an ``asset_id`` is a
    canonical identifier (see ``AssetTarget`` docstring), not a
    filesystem path, so there is nothing to normalize beyond exact,
    case-sensitive comparison — unlike a hostname, there is no DNS-style
    convention that makes two differently-cased identifiers the same
    artifact.
    """
    asset_id = str(target.asset_id)

    if asset_id in scope.denied_assets:
        return (
            False,
            [f"asset '{asset_id}' is explicitly denied (denied_assets)"],
            Severity.BLOCK_ACTION,
        )
    if not scope.allowed_assets:
        return (
            False,
            ["engagement scope has no allowed_assets configured"],
            Severity.BLOCK_ACTION,
        )
    if asset_id in scope.allowed_assets:
        return True, [], None
    return False, [f"asset '{asset_id}' is not within engagement scope"], Severity.BLOCK_ACTION


def check_scope(
    action: ActionProposal, scope: EngagementScope
) -> tuple[bool, list[str], Severity | None]:
    """Returns ``(allowed, reasons, severity)``. Every scope denial is
    ``BLOCK_ACTION``: the agent should try a different (in-scope) target,
    not abandon the goal or the whole run."""
    if isinstance(action.target, AssetTarget):
        return _check_asset_scope(action.target, scope)

    hosts = _target_hosts(action.target)
    if not hosts:
        return (
            False,
            [f"target kind '{action.target.kind}' has no scope-checkable host identity"],
            Severity.BLOCK_ACTION,
        )

    # Deny lists win outright.
    for host in hosts:
        if _matches_host(host, scope.denied_hosts):
            return (
                False,
                [f"target '{host}' is explicitly denied (denied_hosts)"],
                Severity.BLOCK_ACTION,
            )
        if _matches_domain(host, scope.denied_domains):
            return (
                False,
                [f"target '{host}' is explicitly denied (denied_domains)"],
                Severity.BLOCK_ACTION,
            )

    if not (scope.allowed_hosts or scope.allowed_domains or scope.allowed_networks):
        # No explicit allow-list configured: default-deny is safer for a
        # red-team framework than default-allow.
        return (
            False,
            ["engagement scope has no allowed_hosts/domains/networks configured"],
            Severity.BLOCK_ACTION,
        )

    for host in hosts:
        if (
            _matches_host(host, scope.allowed_hosts)
            or _matches_domain(host, scope.allowed_domains)
            or _matches_network(host, scope.allowed_networks)
        ):
            return True, [], None

    return False, [f"target {hosts} is not within engagement scope"], Severity.BLOCK_ACTION
