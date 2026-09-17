"""Verdict -> hypothesis status / finding severity, as one table."""

from __future__ import annotations

from redteam.attack_graph.service import (
    HYPOTHESIS_CONFIRMED,
    HYPOTHESIS_INCONCLUSIVE,
    HYPOTHESIS_OPEN,
    HYPOTHESIS_REJECTED,
)
from redteam.core.enums import RiskLevel, Verdict
from redteam.orchestration.agents.base import PlanStep

HYPOTHESIS_STATUS_BY_VERDICT: dict[Verdict, str] = {
    # A violation confirms the hypothesis that was under test.
    Verdict.VIOLATION: HYPOTHESIS_CONFIRMED,
    # PASS means the expectation did not hold (or held benignly): the
    # security hypothesis was not confirmed, so it is closed, not open.
    Verdict.PASS: HYPOTHESIS_REJECTED,
    # Something unexpected happened — neither confirmed nor cleanly ruled
    # out, and worth a human look.
    Verdict.ANOMALY: HYPOTHESIS_INCONCLUSIVE,
    # No verdict could be reached; the question is still open.
    Verdict.UNKNOWN: HYPOTHESIS_OPEN,
}


def severity_for(step: PlanStep | None) -> str:
    """Severity declared pre-execution by the domain provider, with fallback.

    Tool operational risk is intentionally not used: how dangerous a tool is
    to run is a different axis from the impact of the finding it confirms.
    """
    if step is not None and step.hypothesis is not None:
        if step.hypothesis.severity is not None:
            return step.hypothesis.severity.value
        if step.hypothesis.violation_if_confirmed:
            return RiskLevel.HIGH.value
    return RiskLevel.MEDIUM.value
