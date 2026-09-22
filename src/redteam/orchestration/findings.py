"""Evidence-gated promotion of a verified verdict into a Finding.

    ToolResult -> EvidenceRecord -> Hypothesis evaluation -> Finding

Verification and promotion are different questions and are owned by
different seams on purpose:

- the Analysis verifier (redteam.orchestration.analysis) answers "what does
  this result mean against the expectation recorded before the action ran?"
- the promoter answers "is this claim grounded well enough to assert it?"

Keeping them apart is what stops an over-confident reviewer, or a new
provider that forgot to attach a hypothesis, from turning a bare
observation into a claimed vulnerability. The Attack Graph service already
refuses a *verified* finding with no evidence ids; this seam adds the
positive requirements that a claim needs: a confirmed hypothesis under test
and at least one linked evidence record.

Rationale: docs/agent/orchestration.md (section 10).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Collection
from dataclasses import dataclass, field
from typing import Any

from redteam.attack_graph.service import HYPOTHESIS_CONFIRMED
from redteam.core.contracts.graph import Finding
from redteam.core.enums import Verdict
from redteam.core.ids import new_id


@dataclass(frozen=True)
class PromotionRequest:
    """Everything the promoter needs, with nothing it must go and fetch."""

    verdict: Verdict
    reason: str
    evidence_ids: list[str] = field(default_factory=list)
    hypothesis_id: str | None = None
    hypothesis_status: str = ""
    target_node_id: str = ""
    capability: str = ""
    operation: str = ""
    agent_id: str = ""
    action_id: str = ""
    reviewer_id: str = ""
    source_refs: list[str] = field(default_factory=list)
    severity: str = "info"
    assessment_kind: str = "observation"
    extra_metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PromotionDecision:
    finding: Finding | None
    refused_reason: str = ""

    @property
    def promoted(self) -> bool:
        return self.finding is not None


class FindingPromoter:
    """The single gate between "a reviewer said violation" and "a Finding"."""

    #: A verified claim must point at real evidence records. Raising this
    #: without a corresponding corroboration check would only make the
    #: existing access-control path (baseline + current) fail, so it stays
    #: at the honest minimum: at least one *linked* record.
    minimum_evidence: int = 1

    def claim_key(self, request: PromotionRequest) -> str:
        """Stable identity of the claim, independent of the evidence gathered.

        Two runs that confirm the same hypothesis on the same target through
        the same action are one claim, so the second must not become a second
        Finding. Evidence ids are deliberately excluded: they differ per
        execution and are not part of what is being asserted.
        """

        payload = {
            "capability": request.capability,
            "operation": request.operation,
            "target_node_id": request.target_node_id,
            "hypothesis_id": request.hypothesis_id or "",
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return "finding_claim_" + hashlib.sha256(encoded).hexdigest()[:24]

    def promote(
        self,
        request: PromotionRequest,
        *,
        existing_claims: Collection[str] = (),
    ) -> PromotionDecision:
        if request.assessment_kind != "security_check":
            return PromotionDecision(None, "assessment_is_not_a_security_check")
        if request.verdict is not Verdict.VIOLATION:
            return PromotionDecision(None, "verdict_is_not_a_violation")
        evidence_ids = sorted({str(item) for item in request.evidence_ids if item})
        if len(evidence_ids) < self.minimum_evidence:
            return PromotionDecision(None, "no_linked_evidence")
        if not request.hypothesis_id:
            # A claim nobody asked a question about cannot be traced back to
            # what was under test, so it is not promotable.
            return PromotionDecision(None, "no_hypothesis_under_test")
        if request.hypothesis_status != HYPOTHESIS_CONFIRMED:
            return PromotionDecision(None, "hypothesis_not_confirmed")
        claim_key = self.claim_key(request)
        if claim_key in set(existing_claims):
            return PromotionDecision(None, "duplicate_finding")
        return PromotionDecision(
            Finding(
                finding_id=new_id("find"),
                title=f"{request.capability}/{request.operation} confirmed a violation",
                description=request.reason,
                severity=request.severity,
                verified=True,
                evidence_ids=evidence_ids,
                metadata={
                    "action_id": request.action_id,
                    "agent_id": request.agent_id,
                    "hypothesis_id": request.hypothesis_id,
                    "hypothesis_status": request.hypothesis_status,
                    "target": request.target_node_id,
                    "reviewer_id": request.reviewer_id,
                    "verification_source_refs": list(request.source_refs),
                    "assessment_kind": request.assessment_kind,
                    "claim_key": claim_key,
                    **request.extra_metadata,
                },
            )
        )
