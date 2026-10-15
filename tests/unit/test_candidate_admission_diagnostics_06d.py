"""06D candidate admission diagnostics: owner-produced, bounded, and typed.

Real check_evidence and receipt_failure branches; the evidence port is a fake interface stub.
"""

from types import SimpleNamespace

import pytest

from redteam.orchestration.central_state import (
    RECEIPT_REASONS,
    admission_diagnostic,
    check_evidence,
    receipt_failure,
)
from redteam.orchestration.coordination import CandidateAdmissionConflict
from redteam.orchestration.hypothesis_planner import _candidate_admission_diagnostic


class IndexStore:
    def __init__(self, records, bad=()):
        self.records = records
        self.bad = list(bad)

    async def verify_run_integrity(self, run_id):
        return self.bad

    async def get(self, evidence_id):
        return self.records.get(str(evidence_id))


def record(run="run-a", engagement="eng-a"):
    return SimpleNamespace(run_id=run, engagement_id=engagement, metadata={})


@pytest.mark.asyncio
async def test_missing_original_is_static_owner_diagnostic_with_bounded_evidence_refs():
    with pytest.raises(CandidateAdmissionConflict) as failed:
        await check_evidence(IndexStore({}), run_id="run-a", engagement_id="eng-a",
                             evidence_ids=["ev-missing"])
    assert str(failed.value) == "original_evidence_missing"
    diagnostic = failed.value.diagnostic
    assert diagnostic.owner == "orchestration.central_state"
    assert diagnostic.cause_scope == "evidence_validity"
    assert diagnostic.evidence_ids == ["ev-missing"]
    assert diagnostic.retryable is False


@pytest.mark.asyncio
async def test_scope_mismatch_and_index_disagreement_keep_fixed_branch_codes():
    store = IndexStore({"ev-other": record(run="run-b")})
    with pytest.raises(CandidateAdmissionConflict) as scoped:
        await check_evidence(store, run_id="run-a", engagement_id="eng-a",
                             evidence_ids=["ev-other"])
    assert str(scoped.value) == "evidence_scope_mismatch"
    assert scoped.value.diagnostic.code == "evidence_scope_mismatch"

    with pytest.raises(CandidateAdmissionConflict) as index:
        await check_evidence(IndexStore({}, bad=["ev-x"]), run_id="run-a",
                             engagement_id="eng-a", evidence_ids=["ev-x"])
    assert str(index.value) == "evidence_file_index_disagreement"


def test_receipt_reasons_are_checked_in_order_and_unknown_is_not_proof():
    candidate = SimpleNamespace(status="confirmed", hypothesis_id="hyp-1")
    update = SimpleNamespace(review_evidence_id=None, expected_revision=1)
    assert receipt_failure(update, candidate, None) == "omitted"
    assert set(RECEIPT_REASONS) == {
        "omitted", "not_final", "candidate", "outcome_unknown", "revision", "purpose",
    }


def test_trace_rows_keep_bounded_identifiers_and_drop_unsafe_values():
    kept = admission_diagnostic("code", "s", location="$", repair_condition="r",
                                trace_refs={"candidate_id": "hyp-1", "reason": "omitted"})
    assert kept.trace == [{"candidate_id": "hyp-1", "reason": "omitted"}]
    dropped = admission_diagnostic("code", "s", location="$", repair_condition="r",
                                   trace_refs={"candidate_id": "has space", "reason": None})
    assert dropped.trace == []


def test_planner_projection_keeps_primary_stage_and_bounds_secondary_trace():
    from redteam.orchestration.diagnostics import DecisionDiagnostic

    diagnostics = [
        DecisionDiagnostic(code=f"c{index}", stage="candidate_admission", kind="validation",
                           summary="s", owner="orchestration.central_state")
        for index in range(9)
    ]
    exc = CandidateAdmissionConflict("candidate_verdict_receipt_unbound", diagnostics)
    assert len(exc.diagnostics) == 8
    primary = _candidate_admission_diagnostic(exc, SimpleNamespace(used=2))
    assert primary.code == "c0"
    assert primary.stage == "hypothesis_planning"
    assert primary.attempts == 2
    secondary = primary.trace[-1]["related_admission_diagnostics"]
    assert len(secondary) == 7 and secondary[0]["code"] == "c1"

