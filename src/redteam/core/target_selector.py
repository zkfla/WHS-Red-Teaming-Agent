"""Public target-selector helper for TargetActionPolicy matching.

Root and Control consume this module instead of reaching into private
scope internals. Host, URL and Endpoint spellings of the same target
reach the same restriction through one shared address reading; a
port-less spelling still meets a port-pinned selector entry (fail closed).
Page-resource path and query comparison stays a separate concern and is
never decided here. Matching grants no execution permission by itself.
"""

from __future__ import annotations

from redteam.core.contracts.engagement import EngagementScope
from redteam.core.contracts.target import AnyTarget
from redteam.core.scope_rules import HostPort, target_matches_selector

__all__ = ["HostPort", "selector_matches_target", "target_matches_selector"]


def selector_matches_target(selector: EngagementScope, target: AnyTarget | None) -> bool:
    """Whether target falls inside a TargetActionPolicy selector scope."""
    return target is not None and target_matches_selector(selector, target)
