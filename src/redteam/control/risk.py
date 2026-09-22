"""Action risk resolution for risk-tiered approval (control-team, track-a).

PM direction (problem 3): approval should tighten with the tool's risk
(passive/active/intrusive). The **single source of truth for risk is the
Capability Tool Registry** (``ToolDescriptor.risk_level``) — Control must
NOT keep a hardcoded/duplicated risk table that would drift out of sync.

The read-only ``ToolGateway.list_tools_for_action(capability, operation)``
port (see ``docs/proposals/tool-selection-port.md``) exposes exactly this
metadata to Control without importing the Capability package and without
executing anything. This module reads that snapshot and reduces the
eligible tools to a single risk level (the highest — conservative, since
Orchestration may pick any eligible tool after ALLOW).

The **threshold** ("risk >= X requires approval") is intentionally a
placeholder default here (``DEFAULT_APPROVAL_RISK_THRESHOLD``). Making it
per-engagement configurable (a field on ``ApprovalPolicy``) is a
deliberate follow-up design decision — deferred, not settled yet.
"""

from __future__ import annotations

from typing import Protocol

from redteam.core.enums import RiskLevel
from redteam.core.ports.tool_gateway import ToolGateway

# Total order over the risk ladder so we can compare "at or above".
_RISK_ORDER: dict[RiskLevel, int] = {
    RiskLevel.LOW: 0,
    RiskLevel.MEDIUM: 1,
    RiskLevel.HIGH: 2,
    RiskLevel.CRITICAL: 3,
}

# TEMPORARY default: HIGH or above requires human approval. The real,
# per-engagement threshold is a follow-up design decision (see module doc).
DEFAULT_APPROVAL_RISK_THRESHOLD = RiskLevel.HIGH


def risk_meets_threshold(risk: RiskLevel, threshold: RiskLevel) -> bool:
    """True if ``risk`` is at least as severe as ``threshold``."""
    return _RISK_ORDER[risk] >= _RISK_ORDER[threshold]


class RiskResolver(Protocol):
    """Resolves the risk level of a canonical action (capability, operation)."""

    def resolve(self, capability: str, operation: str) -> RiskLevel | None: ...


class RegistryRiskResolver:
    """Resolves risk from the Capability Tool Registry (source of truth).

    Reads the read-only ``list_tools_for_action`` snapshot and returns the
    **highest** ``risk_level`` among the eligible tools (conservative:
    Orchestration may select any of them after ALLOW). Returns ``None``
    when no tool is registered for the action — the risk stage then has
    nothing to say and approval falls through to the normal path.
    """

    def __init__(self, tool_gateway: ToolGateway) -> None:
        self._gateway = tool_gateway

    def resolve(self, capability: str, operation: str) -> RiskLevel | None:
        tools = self._gateway.list_tools_for_action(capability, operation)
        levels = [tool.risk_level for tool in tools]
        if not levels:
            return None
        return max(levels, key=lambda level: _RISK_ORDER[level])
