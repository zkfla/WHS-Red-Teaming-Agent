"""Independent verification seam used by the Analysis role."""

from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel, Field

from redteam.core.contracts.action import ActionProposal
from redteam.core.contracts.tool import ToolResult
from redteam.core.enums import Verdict
from redteam.orchestration.expectations import Expectation, verdict_for


class VerificationRequest(BaseModel):
    proposal: ActionProposal
    result: ToolResult
    expectation: Expectation | None = None
    source_refs: list[str] = Field(default_factory=list)


class VerificationAssessment(BaseModel):
    verdict: Verdict
    reason: str
    reviewer_id: str
    source_refs: list[str] = Field(default_factory=list)


class AnalysisVerifier(Protocol):
    async def verify(self, request: VerificationRequest) -> VerificationAssessment: ...


class DeterministicAnalysisVerifier:
    """Default reviewer: reproducible and independent from the executor."""

    reviewer_id = "analysis:deterministic-expectation-v1"

    async def verify(self, request: VerificationRequest) -> VerificationAssessment:
        verdict, reason = verdict_for(request.expectation, request.result)
        return VerificationAssessment(
            verdict=verdict,
            reason=reason,
            reviewer_id=self.reviewer_id,
            source_refs=request.source_refs,
        )
