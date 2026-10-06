import sys
import unittest
from dataclasses import replace

sys.path.insert(0, "src")

from redteaming.finding_pipeline import EvidenceGateError, FindingPublisher
from redteaming.models import CandidateStatus, Evidence, Intent, VerificationResult, VulnerabilityCandidate
from redteaming.reporting import render_markdown


class FindingPipelineTests(unittest.TestCase):
    def make_candidate(self):
        return VulnerabilityCandidate(
            category="access_control",
            title="Unauthorized object access",
            asset_ids=("asset_endpoint_1",),
            hypothesis="A lower-privileged identity can access another user's object.",
            test_plan=("Replay the request with a different object identifier.",),
        )

    def test_verified_candidate_becomes_finding(self):
        candidate = self.make_candidate()
        evidence = Evidence(kind="http_exchange", summary="Viewer received another user's object")
        verification = VerificationResult(
            candidate_id=candidate.id,
            status=CandidateStatus.CONFIRMED,
            evidence=(evidence,),
            reproduction_steps=("Authenticate as viewer.", "Request the other object.",),
            observed_impact="Confidential object data is disclosed.",
            confidence=0.96,
        )

        finding = FindingPublisher().publish(candidate, verification, "Enforce object-level authorization.")

        self.assertEqual(finding.status.value, "validated")
        self.assertEqual(finding.severity, "high")
        self.assertIn("Unauthorized object access", render_markdown(finding))

    def test_unverified_candidate_cannot_be_published(self):
        candidate = self.make_candidate()
        verification = VerificationResult(
            candidate_id=candidate.id,
            status=CandidateStatus.INCONCLUSIVE,
            evidence=(),
            reproduction_steps=(),
            observed_impact="",
            confidence=0.99,
        )

        with self.assertRaises(EvidenceGateError):
            FindingPublisher().publish(candidate, verification, "Fix authorization.")

    def test_duplicate_candidate_is_rejected(self):
        candidate = self.make_candidate()
        verification = VerificationResult(
            candidate_id=candidate.id,
            status=CandidateStatus.CONFIRMED,
            evidence=(Evidence(kind="http_exchange", summary="confirmed"),),
            reproduction_steps=("Replay request.",),
            observed_impact="Unauthorized access.",
            confidence=0.90,
        )
        publisher = FindingPublisher()
        publisher.publish(candidate, verification, "Fix authorization.")

        with self.assertRaises(EvidenceGateError):
            publisher.publish(candidate, verification, "Fix authorization.")

    def test_restored_findings_seed_duplicate_guard(self):
        candidate = self.make_candidate()
        verification = VerificationResult(
            candidate_id=candidate.id,
            status=CandidateStatus.CONFIRMED,
            evidence=(Evidence(kind="http_exchange", summary="confirmed"),),
            reproduction_steps=("Replay request.",),
            observed_impact="Unauthorized access.",
            confidence=0.90,
        )
        existing = FindingPublisher().publish(candidate, verification, "Fix authorization.")
        publisher = FindingPublisher()
        publisher.seed_existing([existing])

        with self.assertRaises(EvidenceGateError):
            publisher.publish(candidate, verification, "Fix authorization.")

    def test_distinct_verified_candidates_on_one_asset_are_not_collapsed(self):
        first = self.make_candidate()
        second = replace(first, id="candidate_endpoint_2", title="Another endpoint access")
        publisher = FindingPublisher()
        for candidate in (first, second):
            publisher.publish(
                candidate,
                VerificationResult(
                    candidate_id=candidate.id,
                    status=CandidateStatus.CONFIRMED,
                    evidence=(Evidence(kind="http_exchange", summary=candidate.title),),
                    reproduction_steps=("Replay request.",),
                    observed_impact="Unauthorized access.",
                    confidence=0.90,
                ),
                "Fix authorization.",
            )


if __name__ == "__main__":
    unittest.main()
