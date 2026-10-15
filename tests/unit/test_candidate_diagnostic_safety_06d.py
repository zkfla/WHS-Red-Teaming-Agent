"""06D candidate diagnostic projections accept only safe owner identifiers."""

from redteam.orchestration.central_state import admission_diagnostic
from redteam.orchestration.coordination import (
    CandidateAdmissionConflict,
    safe_identifier,
    safe_identifiers,
)
from redteam.orchestration.diagnostics import DecisionDiagnostic
from redteam.orchestration.specialist_tasks import _candidate_verdict_trace


def _diagnostic(**update) -> DecisionDiagnostic:
    base = DecisionDiagnostic(
        code="candidate_verdict_unproven", stage="candidate_admission", kind="validation",
        summary="A bounded diagnostic.", owner="orchestration.completion",
        repair_condition="Reassess the cited original.", cause_scope="unknown", retryable=False,
        evidence_ids=["ev_good"], criterion_ids=["criterion_good"],
    )
    return base.model_copy(update=update)


def test_safe_identifier_accepts_owner_shapes_and_rejects_free_text():
    assert safe_identifier("ev_3f9c1a2b7e91") == "ev_3f9c1a2b7e91"
    assert safe_identifier("criterion-abc_1") == "criterion-abc_1"
    assert safe_identifier("a" * 100) == "a" * 100
    for value in ["", None, "a" * 101, "ev missing", "token=abc", "ev\nx", "한글", "ev/../x"]:
        assert safe_identifier(value) is None


def test_safe_identifiers_keep_order_dedupe_bound_and_never_substitute():
    raw = ["ev_b", "bad value", "ev_a", "ev_b", "x" * 101, "ev_c"]
    assert safe_identifiers(raw) == ["ev_b", "ev_a", "ev_c"]
    many = [f"ev_{index}" for index in range(15)]
    assert safe_identifiers(many) == many[:10]
    assert safe_identifiers(["bad value", "token=secret"]) == []


def test_admission_diagnostic_drops_unsafe_supplied_evidence_refs():
    leaked = "ev missing " + "A" * 150 + " password=hunter2"
    diagnostic = admission_diagnostic(
        "original_evidence_missing", "A cited evidence original is missing.",
        location="$.evidence_ids", repair_condition="Reassess the original.",
        evidence_ids=[leaked, "ev_kept"],
    )
    assert diagnostic.evidence_ids == ["ev_kept"]
    assert "hunter2" not in diagnostic.model_dump_json()
    assert admission_diagnostic(
        "original_evidence_missing", "Missing.", location="$.evidence_ids",
        repair_condition="Reassess.", evidence_ids=[leaked],
    ).evidence_ids == []


def test_admission_diagnostic_trace_keeps_only_safe_identifier_values():
    diagnostic = admission_diagnostic(
        "candidate_verdict_receipt_unbound", "Unbound.", location="$.updates",
        repair_condition="Replan.", trace_refs={"candidate_id": "hyp_ok", "reason": "bad value"},
    )
    assert diagnostic.trace == [{"candidate_id": "hyp_ok"}]


def test_conflict_projection_drops_unsafe_identifiers_and_keeps_owner_fields():
    unsafe = _diagnostic(evidence_ids=["ev_good", "token=abc"], criterion_ids=["bad criterion"])
    conflict = CandidateAdmissionConflict("candidate_verdict_unproven", [unsafe])
    projected = conflict.diagnostic
    assert projected.evidence_ids == ["ev_good"]
    assert projected.criterion_ids == []
    assert projected.owner == "orchestration.completion"
    assert projected.cause_scope == "unknown"
    assert projected.retryable is False
    assert projected.repair_condition == "Reassess the cited original."
    # The caller's diagnostic object is copied, not edited in place.
    assert unsafe.evidence_ids == ["ev_good", "token=abc"]


def test_candidate_verdict_trace_keeps_safe_candidate_and_review_only():
    assert _candidate_verdict_trace("hyp_1", "ev_review") == [
        {"candidate_id": "hyp_1", "review_evidence_id": "ev_review"},
    ]
    assert _candidate_verdict_trace("hyp 1", "ev review") == []
    assert _candidate_verdict_trace("hyp_1", None) == [{"candidate_id": "hyp_1"}]
