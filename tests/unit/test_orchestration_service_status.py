from redteam.core.contracts.target import HostTarget
from redteam.core.enums import RiskLevel
from redteam.core.ids import ToolId
from redteam.orchestration.agents.base import HypothesisDraft, PlanStep
from redteam.orchestration.service_status import severity_for


def step(hypothesis: HypothesisDraft) -> PlanStep:
    return PlanStep(
        step_id="test",
        capability="analysis",
        operation="response_analysis",
        tool_id=ToolId("analysis.test"),
        target=HostTarget(host="a.local"),
        reason="test",
        hypothesis=hypothesis,
    )


def test_provider_declared_severity_takes_precedence() -> None:
    assert severity_for(
        step(HypothesisDraft(description="critical", severity=RiskLevel.CRITICAL))
    ) == RiskLevel.CRITICAL.value


def test_violation_uses_high_fallback_without_an_override() -> None:
    assert severity_for(
        step(HypothesisDraft(description="violation", violation_if_confirmed=True))
    ) == RiskLevel.HIGH.value
