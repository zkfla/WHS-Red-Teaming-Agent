"""Scope Check — the first pipeline stage in ``DefaultControlPlane.evaluate``.

TODO(control-team): real CIDR/domain-suffix matching (this is exact/
substring matching only), scope-per-capability rules, scope checks for
Principal/Asset/GraphNode target kinds (currently only
host/domain/url/endpoint/service are matched against
``allowed_hosts``/``allowed_domains``/``allowed_networks`` — other
kinds fall through to "not within scope").
"""

from __future__ import annotations

from redteam.core.contracts.action import ActionProposal
from redteam.core.contracts.engagement import EngagementScope
from redteam.core.contracts.target import (
    AnyTarget,
    DomainTarget,
    EndpointTarget,
    HostTarget,
    ServiceTarget,
    UrlTarget,
)


def _scope_candidates(target: AnyTarget) -> set[str]:
    """Strings to compare against allow/deny lists, for target kinds
    that have a host-like identity."""
    if isinstance(target, HostTarget):
        return {target.host}
    if isinstance(target, DomainTarget):
        return {target.domain}
    if isinstance(target, UrlTarget):
        return {target.url}
    if isinstance(target, EndpointTarget):
        return {target.host}
    if isinstance(target, ServiceTarget):
        return {target.host} if target.host else set()
    return set()


def check_scope(action: ActionProposal, scope: EngagementScope) -> tuple[bool, list[str]]:
    candidates = _scope_candidates(action.target)
    if not candidates:
        return False, [f"target kind '{action.target.kind}' has no scope-checkable identity"]

    for denied in [*scope.denied_hosts, *scope.denied_domains]:
        if denied in candidates:
            return False, [f"target '{denied}' is explicitly denied by scope"]

    allow_lists = [*scope.allowed_hosts, *scope.allowed_domains, *scope.allowed_networks]
    if not allow_lists:
        # No explicit allow-list configured: default-deny is safer for a
        # red-team framework than default-allow.
        return False, ["engagement scope has no allowed_hosts/domains/networks configured"]

    for allowed in allow_lists:
        if allowed in candidates or any(c.endswith(allowed) for c in candidates):
            return True, []

    return False, [f"target {sorted(candidates)} is not within engagement scope"]
