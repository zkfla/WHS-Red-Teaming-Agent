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
from math import isfinite
from typing import Any, Literal

from redteam.attack_graph.service import HYPOTHESIS_CONFIRMED
from redteam.core.contracts.assessment import ReviewedFindingDetail
from redteam.core.contracts.evidence import EvidenceRecord
from redteam.core.contracts.graph import Finding, Hypothesis, HypothesisUpdate
from redteam.core.enums import EvidenceSourceType, Verdict
from redteam.core.ids import new_id

MINIMUM_FINDING_CONFIDENCE = 0.8


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
    run_id: str = ""
    claim_identity: str = ""
    extra_metadata: dict[str, Any] = field(default_factory=dict)
    reviewed_detail: ReviewedFindingDetail | None = None


@dataclass(frozen=True)
class PromotionDecision:
    finding: Finding | None
    refused_reason: str = ""

    @property
    def promoted(self) -> bool:
        return self.finding is not None


@dataclass(frozen=True)
class CandidatePromotionInput:
    """Code-derived v2 promotion input, or a stable refusal reason."""

    request: PromotionRequest | None
    refused_reason: str = ""


@dataclass(frozen=True)
class CandidateReviewInput:
    """A final receipt interpreted against its immutable candidate purpose.

    The reviewer owns the semantic outcome.  This value contains only the
    code-checked binding needed by Central State; it grants no storage
    authority by itself.
    """

    status: Literal["confirmed", "rejected", "inconclusive"] | None
    source_ids: tuple[str, ...] = ()
    target_node_id: str = ""
    claim_identity: str = ""
    reviewer_id: str = ""
    confidence: float | None = None
    reviewed_detail: ReviewedFindingDetail | None = None
    origin: dict[str, str] = field(default_factory=dict)
    source_origins: tuple[dict[str, Any], ...] = ()
    refused_reason: str = ""


class FindingPromoter:
    """The single gate between "a reviewer said violation" and "a Finding"."""

    #: A verified claim must point at real evidence records. Raising this
    #: without a corresponding corroboration check would only make the
    #: existing access-control path (baseline + current) fail, so it stays
    #: at the honest minimum: at least one *linked* record.
    minimum_evidence: int = 1

    @staticmethod
    def legacy_claim_key(request: PromotionRequest) -> str:
        """The contract-v1 identity, retained for old callers and cross-path dedup."""
        payload = {
            "capability": request.capability,
            "operation": request.operation,
            "target_node_id": request.target_node_id,
            "hypothesis_id": request.hypothesis_id or "",
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return "finding_claim_" + hashlib.sha256(encoded).hexdigest()[:24]

    def claim_key(self, request: PromotionRequest) -> str:
        """Stable identity of the claim, independent of the evidence gathered.

        Legacy requests retain their historical capability/operation identity.
        A v2 request is scoped by Run, candidate, target and reviewed claim so
        unrelated runs do not over-merge. Evidence ids are deliberately
        excluded: replay may preserve the same claim through different copies.
        """

        if not request.run_id and not request.claim_identity:
            return self.legacy_claim_key(request)
        payload = {
            "contract_version": 2,
            "run_id": request.run_id,
            "target_node_id": request.target_node_id,
            "hypothesis_id": request.hypothesis_id or "",
            "claim_identity": request.claim_identity,
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
        if bool(request.run_id) != bool(request.claim_identity):
            return PromotionDecision(None, "incomplete_v2_claim_identity")
        claim_key = self.claim_key(request)
        if claim_key in set(existing_claims):
            return PromotionDecision(None, "duplicate_finding")
        legacy_key = self.legacy_claim_key(request)
        finding_id = (
            "find_v2_" + hashlib.sha256(claim_key.encode()).hexdigest()[:20]
            if request.run_id and request.claim_identity
            else new_id("find")
        )
        detail = request.reviewed_detail
        title = detail.title if detail is not None else (
            f"{request.capability}/{request.operation} confirmed a violation"
        )
        description = detail.summary if detail is not None else request.reason
        severity = detail.severity if detail is not None else request.severity
        detail_metadata = (
            {"reviewed_finding_detail": detail.model_dump(mode="json")}
            if detail is not None else {}
        )
        return PromotionDecision(
            Finding(
                finding_id=finding_id,
                title=title,
                description=description,
                severity=severity,
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
                    **({"legacy_claim_key": legacy_key} if claim_key != legacy_key else {}),
                    **detail_metadata,
                    **request.extra_metadata,
                },
            )
        )


def claim_keys_from_findings(findings: Collection[Finding]) -> set[str]:
    """All current and compatibility aliases used by either promotion path."""
    keys: set[str] = set()
    for finding in findings:
        metadata = finding.metadata if isinstance(finding.metadata, dict) else {}
        for name in ("claim_key", "legacy_claim_key"):
            value = metadata.get(name)
            if isinstance(value, str) and value:
                keys.add(value)
    return keys


def candidate_review_input(
    *,
    candidate: Hypothesis,
    receipt: EvidenceRecord,
    originals: dict[str, EvidenceRecord],
    run_id: str,
    engagement_id: str,
    task_results: Collection[Any] = (),
) -> CandidateReviewInput:
    """Validate one final review without asking a Planner for the same verdict."""
    from redteam.attack_graph.hypotheses import hypothesis_purpose
    from redteam.orchestration.coordination import content_hash

    meta = receipt.metadata if isinstance(receipt.metadata, dict) else {}
    if candidate.contract_version != 2:
        return CandidateReviewInput(None, refused_reason="candidate_not_v2")
    if str(receipt.run_id) != run_id or str(receipt.engagement_id) != engagement_id:
        return CandidateReviewInput(None, refused_reason="review_receipt_scope_mismatch")
    if (
        receipt.source_type is not EvidenceSourceType.SYSTEM
        or str(receipt.source_id) != str(meta.get("reviewer_id") or "")
        or str(receipt.action_id or "") != str(meta.get("action_id") or "")
        or str(receipt.invocation_id or "") != str(meta.get("invocation_id") or "")
    ):
        return CandidateReviewInput(None, refused_reason="review_receipt_origin_mismatch")
    if (
        meta.get("review_contract_version") not in (2, 3, 4)
        or meta.get("record_kind") != "review_receipt"
        or meta.get("public") is not True
        or meta.get("admission") != "final"
        or meta.get("posthoc") is not True
        or meta.get("origin") != "graph.record"
        or meta.get("assessment_kind") != "security_check"
    ):
        return CandidateReviewInput(None, refused_reason="review_contract_not_applicable")
    contract_version = meta.get("review_contract_version")
    confidence: float | None = None
    if contract_version == 4:
        raw_confidence = meta.get("confidence")
        if isinstance(raw_confidence, bool) or not isinstance(raw_confidence, (int, float)):
            return CandidateReviewInput(None, refused_reason="review_confidence_invalid")
        confidence = float(raw_confidence)
        if not isfinite(confidence) or not 0.0 <= confidence <= 1.0:
            return CandidateReviewInput(None, refused_reason="review_confidence_invalid")
    if (
        str(meta.get("hypothesis_id") or "") != candidate.hypothesis_id
        or meta.get("candidate_revision") != candidate.revision
    ):
        return CandidateReviewInput(None, refused_reason="review_candidate_revision_mismatch")
    if meta.get("candidate_purpose_hash") != content_hash(hypothesis_purpose(candidate)):
        return CandidateReviewInput(None, refused_reason="review_candidate_purpose_mismatch")

    hypothesis_outcome = meta.get("hypothesis_outcome")
    supported_violation = (
        meta.get("outcome") == "success"
        and str(meta.get("verdict", "")).lower() == Verdict.VIOLATION.value
        and meta.get("claim_supported") is True
        and meta.get("security_violation") is True
    )
    if (
        hypothesis_outcome == "success"
        and supported_violation
        and (contract_version in (2, 3) or confidence >= MINIMUM_FINDING_CONFIDENCE)
    ):
        status: Literal["confirmed", "rejected", "inconclusive"] = "confirmed"
    elif hypothesis_outcome == "refuted":
        if meta.get("security_violation") is True:
            return CandidateReviewInput(None, refused_reason="review_outcome_contradictory")
        status = "rejected"
    elif hypothesis_outcome in (None, "unknown", "inconclusive", "blocked"):
        status = "inconclusive"
    elif hypothesis_outcome == "success":
        # A benign, unsupported, or incomplete claim cannot confirm a security
        # candidate in the posthoc security-check path.
        status = "inconclusive"
    else:
        return CandidateReviewInput(None, refused_reason="review_outcome_invalid")

    target = str(meta.get("target_node_id") or "").strip()
    claim_identity = str(meta.get("claim_identity") or "").strip()
    reviewer_id = str(meta.get("reviewer_id") or "").strip()
    if (
        not target
        or target not in candidate.target_node_ids
        or len(claim_identity) != 64
        or any(char not in "0123456789abcdef" for char in claim_identity)
        or not reviewer_id
    ):
        return CandidateReviewInput(None, refused_reason="review_claim_identity_invalid")

    raw_principals = meta.get("claimed_principals", [])
    if not isinstance(raw_principals, (list, tuple, set)):
        return CandidateReviewInput(None, refused_reason="review_claim_identity_invalid")
    claimed_principals = sorted({
        str(item).strip() for item in raw_principals
        if isinstance(item, str) and item.strip()
    })
    if set(claimed_principals) - set(candidate.principal_refs):
        return CandidateReviewInput(None, refused_reason="review_principal_outside_candidate")
    expected_claim_identity = content_hash({
        "statement": " ".join(str(meta.get("claim") or "").split()),
        "target_node_id": target,
        "principal_refs": claimed_principals,
    })
    if claim_identity != expected_claim_identity:
        return CandidateReviewInput(None, refused_reason="review_claim_identity_mismatch")

    raw_source_ids = meta.get("source_evidence_ids", [])
    if not isinstance(raw_source_ids, (list, tuple)):
        return CandidateReviewInput(None, refused_reason="review_source_identity_invalid")
    source_ids = tuple(
        str(item).strip() for item in raw_source_ids
        if isinstance(item, str) and item.strip()
    )
    if not source_ids or len(source_ids) != len(set(source_ids)):
        return CandidateReviewInput(None, refused_reason="review_source_identity_invalid")

    origins = meta.get("source_origins")
    if not isinstance(origins, list) or len(origins) != len(source_ids):
        return CandidateReviewInput(None, refused_reason="review_source_origins_invalid")
    by_origin = {
        str(item.get("evidence_id") or ""): item
        for item in origins if isinstance(item, dict)
    }
    if set(by_origin) != set(source_ids):
        return CandidateReviewInput(None, refused_reason="review_source_origins_invalid")
    primary: tuple[EvidenceRecord, dict[str, str]] | None = None
    for source_id in source_ids:
        source = originals.get(source_id)
        origin = by_origin[source_id]
        if source is None or (
            str(source.run_id) != run_id
            or str(source.engagement_id) != engagement_id
            or not str(source.action_id or "").strip()
            or not str(source.invocation_id or "").strip()
            or str(origin.get("action_id") or "") != str(source.action_id)
            or str(origin.get("invocation_id") or "") != str(source.invocation_id)
        ):
            return CandidateReviewInput(None, refused_reason="review_source_origin_mismatch")
        source_meta = source.metadata if isinstance(source.metadata, dict) else {}
        proposal = source_meta.get("proposal")
        proposal = proposal if isinstance(proposal, dict) else {}
        expected_origin = {
            "target_node_id": str(source_meta.get("target_node_id") or ""),
            "capability": str(source_meta.get("capability") or proposal.get("capability") or ""),
            "operation": str(source_meta.get("operation") or proposal.get("operation") or ""),
            "agent_id": str(source_meta.get("agent_id") or proposal.get("agent_id") or ""),
        }
        if any(str(origin.get(name) or "") != value for name, value in expected_origin.items()):
            return CandidateReviewInput(None, refused_reason="review_source_origin_mismatch")
        if (
            str(source.action_id) == str(meta.get("action_id") or "")
            and str(source.invocation_id) == str(meta.get("invocation_id") or "")
        ):
            primary = (source, expected_origin)
    if primary is None:
        return CandidateReviewInput(None, refused_reason="review_primary_origin_missing")

    reviewed_detail = None
    matched_draft = None
    if contract_version == 3 or (
        contract_version == 4 and meta.get("reviewed_finding_detail") is not None
    ):
        try:
            reviewed_detail = ReviewedFindingDetail.model_validate(
                meta.get("reviewed_finding_detail")
            )
        except Exception:
            return CandidateReviewInput(None, refused_reason="reviewed_finding_detail_invalid")
        binding = reviewed_detail.draft_binding
        if reviewed_detail.statement != str(meta.get("claim") or ""):
            return CandidateReviewInput(
                None, refused_reason="reviewed_finding_detail_claim_mismatch"
            )
        for task in task_results:
            result = getattr(task, "result", None)
            if result is None or str(result.result_id) != binding.result_id:
                continue
            for draft in result.assessment_drafts:
                if draft.draft_id == binding.draft_id and draft.identity(
                    binding.result_id
                )["content_hash"] == binding.content_hash:
                    matched_draft = draft
                    break
            if matched_draft is not None:
                break
        if matched_draft is None:
            return CandidateReviewInput(
                status,
                source_ids=source_ids,
                target_node_id=target,
                claim_identity=claim_identity,
                reviewer_id=reviewer_id,
                confidence=confidence,
                reviewed_detail=reviewed_detail,
                origin=primary[1],
                source_origins=tuple(origins),
                refused_reason="reviewed_assessment_draft_not_stored",
            )
        if (
            matched_draft.statement != reviewed_detail.statement
            or matched_draft.target_node_id != target
            or matched_draft.hypothesis_id != candidate.hypothesis_id
            or sorted(matched_draft.principal_refs) != claimed_principals
        ):
            return CandidateReviewInput(
                None, refused_reason="reviewed_assessment_draft_binding_mismatch"
            )
        if set(matched_draft.original_evidence_ids()) != set(source_ids):
            return CandidateReviewInput(
                None, refused_reason="reviewed_assessment_draft_source_mismatch"
            )
        if {ref.evidence_id for ref in reviewed_detail.evidence_refs} - set(source_ids):
            return CandidateReviewInput(
                None, refused_reason="reviewed_finding_detail_source_mismatch"
            )

    return CandidateReviewInput(
        status,
        source_ids=source_ids,
        target_node_id=target,
        claim_identity=claim_identity,
        reviewer_id=reviewer_id,
        confidence=confidence,
        reviewed_detail=reviewed_detail,
        origin=primary[1],
        source_origins=tuple(origins),
    )


def promotion_request_from_review(
    *, review: CandidateReviewInput, committed: Hypothesis, receipt: EvidenceRecord,
) -> PromotionRequest:
    """Build the promoter input only after Central State chose the transition."""
    if review.status != "confirmed" or review.refused_reason:
        raise ValueError("candidate review is not promotable")
    meta = receipt.metadata if isinstance(receipt.metadata, dict) else {}
    return PromotionRequest(
        verdict=Verdict.VIOLATION,
        reason=str(meta.get("reason") or "verified security violation")[:500],
        evidence_ids=[*review.source_ids, str(receipt.evidence_id)],
        hypothesis_id=committed.hypothesis_id,
        hypothesis_status=committed.status,
        target_node_id=review.target_node_id,
        capability=review.origin.get("capability") or "runtime",
        operation=review.origin.get("operation") or "observed_security_claim",
        agent_id=review.origin.get("agent_id", ""),
        action_id=str(meta.get("action_id") or ""),
        reviewer_id=review.reviewer_id,
        source_refs=list(review.source_ids),
        severity=review.reviewed_detail.severity if review.reviewed_detail else "medium",
        assessment_kind="security_check",
        run_id=str(receipt.run_id),
        claim_identity=review.claim_identity,
        reviewed_detail=review.reviewed_detail,
        extra_metadata={
            "promotion_contract_version": 2,
            "claim_identity": review.claim_identity,
            "review_receipt_id": str(receipt.evidence_id),
            "candidate_pre_verdict_revision": committed.revision - 1,
            "candidate_revision": committed.revision,
            "candidate_purpose_hash": str(meta.get("candidate_purpose_hash") or ""),
            "source_origins": list(review.source_origins),
            "outcome": "success",
            "claim_supported": True,
            "review_confidence": review.confidence,
            "minimum_finding_confidence": MINIMUM_FINDING_CONFIDENCE,
            "invocation_id": str(meta.get("invocation_id") or ""),
        },
    )


def candidate_promotion_input(
    *,
    update: HypothesisUpdate,
    committed: Hypothesis,
    receipt: EvidenceRecord,
    originals: dict[str, EvidenceRecord],
    run_id: str,
    engagement_id: str,
    task_results: Collection[Any] = (),
) -> CandidatePromotionInput:
    """Cross-check a confirmed v2 transition and build the sole promoter request.

    The receipt's prose never establishes trust. Identity, scope, origin and the
    security semantics are checked structurally against inspected originals.
    """
    if committed.contract_version != 2 or committed.status != HYPOTHESIS_CONFIRMED:
        return CandidatePromotionInput(None, "candidate_not_confirmed_v2")
    if update.expected_revision is None or committed.revision != update.expected_revision + 1:
        return CandidatePromotionInput(None, "candidate_transition_mismatch")
    if str(receipt.evidence_id) != str(update.review_evidence_id or ""):
        return CandidatePromotionInput(None, "review_receipt_mismatch")
    before = committed.model_copy(update={"revision": update.expected_revision})
    review = candidate_review_input(
        candidate=before,
        receipt=receipt,
        originals=originals,
        run_id=run_id,
        engagement_id=engagement_id,
        task_results=task_results,
    )
    if review.refused_reason:
        return CandidatePromotionInput(None, review.refused_reason)
    if review.status != "confirmed":
        return CandidatePromotionInput(None, "review_did_not_confirm_security_violation")
    if set(review.source_ids) - set(committed.supporting_evidence_ids):
        return CandidatePromotionInput(None, "review_sources_not_candidate_support")
    return CandidatePromotionInput(promotion_request_from_review(
        review=review, committed=committed, receipt=receipt,
    ))
