"""Deterministic Tool Selection policy over ``ToolCatalog.list_tools_for_action``.

Orchestration never hardcodes a ``ToolId``: ``CandidateCompiler`` asks the
live Capability Registry (via ``ToolCatalog``/``ToolGateway.list_tools_for_action``)
which registered tools are eligible for a ``<capability>.<operation>``
action, then this module picks one, deterministically, from that live set.

Priority (``docs/agent/orchestration-implementation-plan.md`` section 6,
``docs/proposals/tool-selection-port.md``):

    healthy > unknown                     (unhealthy is already excluded
                                            by ``list_tools_for_action``)
    lower risk_level > higher risk_level
    stable tool_id lexical order          (final tie-break)

Historical failure rate is intentionally not a tier here: no per-tool
failure signal exists yet anywhere in the codebase (``WorkItem`` tracks
candidates, not bound tools), and inventing one would be exactly the kind
of unsupported-but-silently-passing signal this task was told not to add.
"""

from __future__ import annotations

from redteam.core.contracts.tool import ToolDescriptor
from redteam.core.enums import RiskLevel, ToolHealthStatus

_HEALTH_RANK = {
    ToolHealthStatus.HEALTHY: 0,
    ToolHealthStatus.UNKNOWN: 1,
    ToolHealthStatus.UNHEALTHY: 2,  # never reached: excluded upstream
}

_RISK_RANK = {
    RiskLevel.LOW: 0,
    RiskLevel.MEDIUM: 1,
    RiskLevel.HIGH: 2,
    RiskLevel.CRITICAL: 3,
}


def select_tool(candidates: list[ToolDescriptor]) -> ToolDescriptor | None:
    """Deterministically pick one eligible ``ToolDescriptor``, or ``None``.

    ``candidates`` is expected to already be the output of
    ``list_tools_for_action`` (namespace/operation/health already
    filtered) — this function only breaks ties, it does not re-check
    eligibility.
    """
    if not candidates:
        return None
    return min(
        candidates,
        key=lambda tool: (
            _HEALTH_RANK.get(tool.health_status, 1),
            _RISK_RANK.get(tool.risk_level, 1),
            str(tool.tool_id),
        ),
    )
