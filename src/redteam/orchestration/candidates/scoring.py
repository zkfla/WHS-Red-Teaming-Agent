"""Explainable, deterministic base score.  LLMs may rerank only ties/gaps."""

from __future__ import annotations

from redteam.core.enums import RiskLevel, ToolHealthStatus
from redteam.orchestration.candidates.models import (
    CandidateDraft,
    PriorityScore,
)

DEFAULT_WEIGHTS = {
    "expected_information_gain": 2.0,
    "objective_progress": 1.8,
    "evidence_gap_reduction": 1.4,
    "downstream_unblock_count": 1.0,
    "historical_success": 0.8,
    "impact": 1.0,
    "estimated_difficulty": -1.4,
    "expected_cost": -1.0,
    "operational_risk": -1.2,
    "repetition_penalty": -2.0,
}

_RISK_PENALTY = {
    RiskLevel.LOW: 0.0,
    RiskLevel.MEDIUM: 0.25,
    RiskLevel.HIGH: 0.65,
    RiskLevel.CRITICAL: 1.0,
}

_HEALTH_ADJUSTMENT = {
    ToolHealthStatus.HEALTHY: 0.1,
    ToolHealthStatus.UNKNOWN: 0.0,
    ToolHealthStatus.UNHEALTHY: -10.0,
}


def score_candidate(
    draft: CandidateDraft,
    *,
    risk_level: RiskLevel,
    health_status: ToolHealthStatus,
    weights: dict[str, float] | None = None,
) -> PriorityScore:
    resolved = {**DEFAULT_WEIGHTS, **(weights or {})}
    values = draft.signals.model_dump()
    values["operational_risk"] = max(float(values["operational_risk"]), _RISK_PENALTY[risk_level])
    components: dict[str, float] = {}
    for name, weight in resolved.items():
        value = values[name]
        if name == "downstream_unblock_count":
            value = min(int(value), 10) / 10
        components[name] = round(float(value) * weight, 6)
    components["tool_health"] = _HEALTH_ADJUSTMENT[health_status]
    return PriorityScore(total=round(sum(components.values()), 6), components=components)
