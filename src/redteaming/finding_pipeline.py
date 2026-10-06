from __future__ import annotations

from dataclasses import dataclass

from .models import Finding, VulnerabilityCandidate, VerificationResult, CandidateStatus


class EvidenceGateError(ValueError):
    pass


@dataclass(frozen=True)
class FindingPolicy:
    minimum_confidence: float = 0.85


class FindingPublisher:
    """Promotes only verified, evidence-backed candidates to findings."""

    def __init__(self, policy: FindingPolicy | None = None) -> None:
        self.policy = policy or FindingPolicy()
        self._published_keys: set[tuple[str, tuple[str, ...], str]] = set()
        self._legacy_keys: set[tuple[str, tuple[str, ...]]] = set()

    def seed_existing(self, findings: list[Finding] | tuple[Finding, ...]) -> None:
        """Restore duplicate protection when an engagement snapshot is loaded."""
        for item in findings:
            base = (item.category, tuple(sorted(item.asset_ids)))
            if item.candidate_id:
                self._published_keys.add((*base, item.candidate_id))
            else:
                # Old snapshots did not retain candidate IDs. Preserve their
                # conservative duplicate behavior while allowing all new
                # endpoint-level Findings to remain distinct.
                self._legacy_keys.add(base)

    def publish(
        self,
        candidate: VulnerabilityCandidate,
        verification: VerificationResult,
        remediation: str,
    ) -> Finding:
        if verification.candidate_id != candidate.id:
            raise EvidenceGateError("verification does not belong to candidate")
        if verification.status is not CandidateStatus.CONFIRMED:
            raise EvidenceGateError("candidate is not verified")
        if verification.confidence < self.policy.minimum_confidence:
            raise EvidenceGateError("verification confidence is below the publication threshold")
        if not verification.evidence:
            raise EvidenceGateError("validated finding requires evidence")
        if not verification.reproduction_steps:
            raise EvidenceGateError("validated finding requires reproduction steps")
        if not verification.observed_impact.strip():
            raise EvidenceGateError("validated finding requires observed impact")
        if not candidate.asset_ids:
            raise EvidenceGateError("validated finding requires an affected asset")

        base = (candidate.category, tuple(sorted(candidate.asset_ids)))
        key = (*base, candidate.id)
        if key in self._published_keys or base in self._legacy_keys:
            raise EvidenceGateError("duplicate finding")
        self._published_keys.add(key)

        return Finding(
            title=candidate.title,
            category=candidate.category,
            severity=self._severity(verification.confidence),
            asset_ids=candidate.asset_ids,
            reproduction_steps=verification.reproduction_steps,
            impact=verification.observed_impact,
            evidence_ids=tuple(item.id for item in verification.evidence),
            remediation=remediation,
            candidate_id=candidate.id,
            root_cause=candidate.root_cause,
        )

    @staticmethod
    def _severity(confidence: float) -> str:
        if confidence >= 0.95:
            return "high"
        if confidence >= 0.90:
            return "medium"
        return "low"
