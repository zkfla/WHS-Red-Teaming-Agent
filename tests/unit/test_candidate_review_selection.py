"""Planner-side candidate review selection guard with real candidate and receipt contracts.

No planner, store or model is faked here. The receipt verdicts come from the central
receipt_failure rules; this file checks that the planner guard reports each one as a safe,
indexed, retryable model-input repair before any admission write.
"""

import json
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from redteam.attack_graph.hypotheses import hypothesis_purpose
from redteam.core.contracts.evidence import EvidenceRecord
from redteam.core.contracts.graph import CandidateHypothesis, HypothesisUpdate
from redteam.core.enums import EvidenceSourceType
from redteam.core.ids import EngagementId, EvidenceId, RunId
from redteam.orchestration.central_state import receipt_failure
from redteam.orchestration.coordination import content_hash
from redteam.orchestration.hypothesis_planner import (
    HypothesisDecision,
    HypothesisProposalError,
    _split_plan_response_schema,
    planner_candidate_wire_schema,
    validate_candidate_review_selections,
    validate_candidate_status_presence,
)
from redteam.orchestration.probe_atomization import HypothesisPlanSelection

TARGET = "host:unit.local"
SECRET = "SECRET-REVIEW-MARKER-7f3a"
RUN, ENGAGEMENT = RunId("run-review-unit"), EngagementId("eng-review-unit")
RECEIPT = "ev-review-receipt"
OUTCOME = {"confirmed": "success", "rejected": "refuted"}


def candidate(status="confirmed", key="local", premise=None):
    return CandidateHypothesis(
        contract_version=2, hypothesis_id=key, description="Access may be unguarded",
        premise=premise, target_node_ids=[TARGET], reason="Assess the preserved response",
        status=status,
    )


def update(cand, *, revision=1, review=RECEIPT):
    return HypothesisUpdate(hypothesis=cand, expected_revision=revision, review_evidence_id=review)


def record(rid=RECEIPT, *, source="orchestration.graph_record_review", **meta):
    return EvidenceRecord(
        evidence_id=EvidenceId(rid), run_id=RUN, engagement_id=ENGAGEMENT,
        source_type=EvidenceSourceType.SYSTEM, source_id=source, content_type="application/json",
        digest="0" * 64, summary="Independent review record", created_at=datetime.now(timezone.utc),
        metadata=meta,
    )


def receipt(cand, *, revision=1, rid=RECEIPT, **overrides):
    """A final public review receipt bound to this candidate, revision and purpose."""
    meta = {
        "record_kind": "review_receipt", "public": True, "admission": "final",
        "verdict": "supported", "outcome": OUTCOME[cand.status],
        "hypothesis_outcome": OUTCOME[cand.status], "hypothesis_id": cand.hypothesis_id,
        "candidate_revision": revision,
        "candidate_purpose_hash": content_hash(hypothesis_purpose(cand)), "reason": SECRET,
    }
    meta.update(overrides)
    return record(rid, **meta)


def task_receipt(rid=RECEIPT):
    return record(rid, source="orchestration.task_completion_receipt",
                  record_kind="task_completion_receipt", task_id="task-1", reason=SECRET)


def assert_repair(caught, *, index, reason):
    error = caught.value
    assert error.code == "candidate_review_receipt_invalid"
    diagnostic = error.diagnostic
    assert diagnostic.location == f"$.updates[{index}].review_evidence_id"
    assert diagnostic.retryable is True
    assert diagnostic.cause_scope == "model_input"
    assert diagnostic.stage == "hypothesis_planning"
    assert diagnostic.trace == [{"receipt_failure": reason}]
    dumped = json.dumps(diagnostic.model_dump(mode="json"))
    assert SECRET not in dumped and RECEIPT not in dumped
    assert str(error) == "candidate_review_receipt_invalid"


def _case(name):
    cand = candidate()
    other = candidate(key="other")
    rejected = candidate(status="rejected")
    cases = {
        "task_completion_receipt": (update(cand), {RECEIPT: task_receipt()}, "not_final"),
        "missing_record": (update(cand), {}, "omitted"),
        "nonpublic": (update(cand), {RECEIPT: receipt(cand, public=False)}, "not_final"),
        "posthoc_not_final": (update(cand), {RECEIPT: receipt(cand, admission="posthoc")},
                              "not_final"),
        "other_candidate": (update(cand), {RECEIPT: receipt(other)}, "candidate"),
        "stale_revision": (update(cand), {RECEIPT: receipt(cand, revision=2)}, "revision"),
        "other_purpose": (update(cand), {RECEIPT: receipt(
            candidate(premise="A different prerequisite"))}, "purpose"),
        "unknown_verdict": (update(cand), {RECEIPT: receipt(cand, verdict="unknown")},
                            "outcome_unknown"),
        "opposite_outcome": (update(cand), {RECEIPT: receipt(
            cand, hypothesis_outcome="refuted", outcome="refuted")}, "outcome_unknown"),
        "rejected_with_success": (update(rejected), {RECEIPT: receipt(
            rejected, hypothesis_outcome="success", outcome="success")}, "outcome_unknown"),
    }
    return cases[name]


CASE_NAMES = [
    "task_completion_receipt", "missing_record", "nonpublic", "posthoc_not_final",
    "other_candidate", "stale_revision", "other_purpose", "unknown_verdict",
    "opposite_outcome", "rejected_with_success",
]


@pytest.mark.parametrize("name", CASE_NAMES)
def test_wrong_receipt_is_a_safe_indexed_model_input_repair(name):
    selected, records, expected = _case(name)
    cand = selected.hypothesis
    # The shared receipt rules name the same reason the planner guard reports.
    assert receipt_failure(selected, cand, records.get(RECEIPT)) == expected
    with pytest.raises(HypothesisProposalError) as caught:
        validate_candidate_review_selections([selected], records)
    assert_repair(caught, index=0, reason=expected)


@pytest.mark.parametrize("status", ["confirmed", "rejected"])
def test_bound_final_receipt_passes_for_each_verdict(status):
    cand = candidate(status=status)
    validate_candidate_review_selections([update(cand)], {RECEIPT: receipt(cand)})


@pytest.mark.parametrize("status", ["open", "inconclusive", "held"])
def test_nonverdict_update_without_review_evidence_remains_valid(status):
    cand = candidate(status=status)
    validate_candidate_review_selections(
        [HypothesisUpdate(hypothesis=cand, expected_revision=None)], {},
    )


def test_first_invalid_verdict_is_the_reported_index():
    good, bad = candidate(key="good"), candidate(key="bad")
    updates = [
        HypothesisUpdate(hypothesis=candidate(status="open", key="open"),
                         expected_revision=None),
        update(good, review="ev-good"),
        update(bad, review="ev-missing"),
    ]
    records = {"ev-good": receipt(good, rid="ev-good")}
    with pytest.raises(HypothesisProposalError) as caught:
        validate_candidate_review_selections(updates, records)
    assert_repair(caught, index=2, reason="omitted")


def test_missing_review_id_on_a_verdict_is_rejected_by_the_update_contract_first():
    with pytest.raises(ValidationError):
        HypothesisUpdate(hypothesis=candidate(), expected_revision=1)


@pytest.mark.parametrize("model,split", [
    (HypothesisDecision, False),
    (HypothesisPlanSelection, True),
])
def test_active_planner_wire_schema_requires_candidate_status(model, split):
    schema = planner_candidate_wire_schema(model.model_json_schema())
    if split:
        schema = _split_plan_response_schema(schema)
    ref = schema["$defs"]["HypothesisUpdate"]["properties"]["hypothesis"]["$ref"]
    candidate_schema = schema["$defs"][ref.rsplit("/", 1)[-1]]

    assert "status" in candidate_schema["required"]
    assert "default" not in candidate_schema["properties"]["status"]
    assert "confidence" not in candidate_schema["required"]


def test_planner_rejects_omitted_status_before_domain_default_but_accepts_open():
    hypothesis = candidate(status="open").model_dump(mode="json")
    hypothesis.pop("status")
    raw = {"updates": [{"hypothesis": hypothesis}], "reason": "Check a captured premise"}

    # Both active decision models materialize the domain default unless the raw wire
    # payload is checked first.
    assert HypothesisDecision.model_validate(raw).updates[0].hypothesis.status == "open"
    assert HypothesisPlanSelection.model_validate(raw).updates[0].hypothesis.status == "open"
    with pytest.raises(HypothesisProposalError) as caught:
        validate_candidate_status_presence(raw)
    assert caught.value.code == "candidate_status_required"
    assert caught.value.diagnostic.location == "$.updates[0].hypothesis.status"

    hypothesis["status"] = "open"
    validate_candidate_status_presence({"updates": [{"hypothesis": hypothesis}]})
    assert CandidateHypothesis.model_validate(hypothesis).status == "open"


@pytest.mark.parametrize("status", ["open", "inconclusive", "held"])
def test_nonterminal_update_cannot_cite_a_review_receipt(status):
    selected = HypothesisUpdate(
        hypothesis=candidate(status=status), expected_revision=1, review_evidence_id=RECEIPT,
    )

    with pytest.raises(HypothesisProposalError) as caught:
        validate_candidate_review_selections([selected], {RECEIPT: receipt(
            candidate(status="confirmed"),
        )})

    diagnostic = caught.value.diagnostic
    assert caught.value.code == "candidate_review_receipt_requires_terminal_status"
    assert diagnostic.location == "$.updates[0].review_evidence_id"
    assert diagnostic.retryable is True and diagnostic.cause_scope == "model_input"
    rendered = json.dumps(diagnostic.model_dump(mode="json"))
    assert SECRET not in rendered and RECEIPT not in rendered
