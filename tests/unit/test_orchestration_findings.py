"""A Finding is a claim, so promotion is gated separately from verification.

The verifier answers "what does this result mean"; the promoter answers "is
this grounded well enough to assert". These tests pin the second question,
because an over-confident reviewer or a provider that forgot to attach a
hypothesis must not be able to turn an observation into a vulnerability.
"""

from __future__ import annotations

from typing import Any

from redteam.attack_graph.service import (
    HYPOTHESIS_CONFIRMED,
    HYPOTHESIS_INCONCLUSIVE,
    HYPOTHESIS_REJECTED,
)
from redteam.core.enums import Verdict
from redteam.orchestration.findings import FindingPromoter, PromotionRequest


def request(**overrides) -> PromotionRequest:
    values: dict[str, Any] = {
        "verdict": Verdict.VIOLATION,
        "reason": "anonymous 401 baseline reached with an operator-declared deny session",
        "evidence_ids": ["ev_baseline", "ev_session"],
        "hypothesis_id": "hyp_1",
        "hypothesis_status": HYPOTHESIS_CONFIRMED,
        "target_node_id": "endpoint:a.local GET /",
        "capability": "web",
        "operation": "http_request",
        "agent_id": "web-api-agent",
        "action_id": "action_1",
        "reviewer_id": "analysis:deterministic-expectation-v1",
        "severity": "high",
        "assessment_kind": "security_check",
    }
    values.update(overrides)
    return PromotionRequest(**values)


def test_a_grounded_violation_becomes_a_verified_finding() -> None:
    decision = FindingPromoter().promote(request())

    assert decision.promoted
    finding = decision.finding
    assert finding is not None
    assert finding.verified is True
    assert finding.evidence_ids == ["ev_baseline", "ev_session"]
    assert finding.severity == "high"
    # The claim is traceable back to what was under test and where it landed.
    assert finding.metadata["hypothesis_id"] == "hyp_1"
    assert finding.metadata["target"] == "endpoint:a.local GET /"
    assert finding.metadata["claim_key"]


def test_a_violation_without_linked_evidence_is_refused() -> None:
    decision = FindingPromoter().promote(request(evidence_ids=[]))

    assert not decision.promoted
    assert decision.refused_reason == "no_linked_evidence"


def test_blank_evidence_ids_do_not_count_as_evidence() -> None:
    decision = FindingPromoter().promote(request(evidence_ids=["", ""]))

    assert not decision.promoted
    assert decision.refused_reason == "no_linked_evidence"


def test_a_violation_with_no_hypothesis_under_test_is_refused() -> None:
    # Nothing was asked, so there is nothing to trace the claim back to.
    decision = FindingPromoter().promote(request(hypothesis_id=None))

    assert not decision.promoted
    assert decision.refused_reason == "no_hypothesis_under_test"


def test_a_reviewers_violation_is_refused_when_the_hypothesis_is_not_confirmed() -> None:
    # This is the over-confident-reviewer case: the verdict says violation but
    # the question under test was never actually settled.
    for status in (HYPOTHESIS_REJECTED, HYPOTHESIS_INCONCLUSIVE, ""):
        decision = FindingPromoter().promote(request(hypothesis_status=status))
        assert not decision.promoted
        assert decision.refused_reason == "hypothesis_not_confirmed"


def test_a_non_violation_verdict_is_never_promoted() -> None:
    for verdict in (Verdict.PASS, Verdict.ANOMALY, Verdict.UNKNOWN):
        decision = FindingPromoter().promote(request(verdict=verdict))
        assert not decision.promoted
        assert decision.refused_reason == "verdict_is_not_a_violation"


def test_an_observation_assessment_can_never_be_promoted() -> None:
    decision = FindingPromoter().promote(request(assessment_kind="observation"))

    assert not decision.promoted
    assert decision.refused_reason == "assessment_is_not_a_security_check"


def test_the_same_claim_is_not_promoted_twice() -> None:
    promoter = FindingPromoter()
    first = promoter.promote(request())
    assert first.promoted
    assert first.finding is not None
    claim_key = first.finding.metadata["claim_key"]

    second = promoter.promote(request(evidence_ids=["ev_other"]), existing_claims={claim_key})

    assert not second.promoted
    assert second.refused_reason == "duplicate_finding"


def test_claim_identity_ignores_which_evidence_was_collected() -> None:
    promoter = FindingPromoter()
    a = promoter.claim_key(request(evidence_ids=["ev_1"]))
    b = promoter.claim_key(request(evidence_ids=["ev_1", "ev_2"]))

    assert a == b
    assert promoter.claim_key(request(target_node_id="endpoint:other GET /")) != a
    assert promoter.claim_key(request(operation="auth_attempt")) != a


def test_the_finding_is_ordered_evidence_deduplicated() -> None:
    decision = FindingPromoter().promote(
        request(evidence_ids=["ev_b", "ev_a", "ev_b"])
    )

    assert decision.finding is not None
    assert decision.finding.evidence_ids == ["ev_a", "ev_b"]


def test_v2_claim_identity_is_run_scoped_and_deterministic() -> None:
    promoter = FindingPromoter()
    first = promoter.promote(request(run_id="run_1", claim_identity="a" * 64))
    replay = promoter.promote(request(
        run_id="run_1", claim_identity="a" * 64, evidence_ids=["ev_replayed"],
    ))

    assert first.finding is not None and replay.finding is not None
    assert first.finding.finding_id == replay.finding.finding_id
    assert first.finding.metadata["claim_key"] == replay.finding.metadata["claim_key"]
    assert first.finding.metadata["legacy_claim_key"]
    assert promoter.claim_key(request(run_id="run_2", claim_identity="a" * 64)) != (
        first.finding.metadata["claim_key"]
    )


def test_v2_identity_must_supply_run_and_claim_together() -> None:
    for overrides in ({"run_id": "run_1"}, {"claim_identity": "a" * 64}):
        decision = FindingPromoter().promote(request(**overrides))
        assert decision.finding is None
        assert decision.refused_reason == "incomplete_v2_claim_identity"


def test_v2_finding_exposes_legacy_alias_for_cross_path_deduplication() -> None:
    promoter = FindingPromoter()
    v2 = promoter.promote(request(run_id="run_1", claim_identity="a" * 64))
    assert v2.finding is not None

    legacy = promoter.promote(
        request(), existing_claims={v2.finding.metadata["legacy_claim_key"]},
    )

    assert legacy.finding is None
    assert legacy.refused_reason == "duplicate_finding"
