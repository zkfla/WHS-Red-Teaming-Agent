from __future__ import annotations

from datetime import datetime, timezone

from redteam.core.contracts.evidence import EvidenceRecord
from redteam.core.contracts.target import HostTarget
from redteam.core.enums import EvidenceSourceType
from redteam.core.ids import (
    ActionId,
    EngagementId,
    EvidenceId,
    HypothesisId,
    InvocationId,
    RunId,
)
from redteam.evidence.validation import (
    CheckStatus,
    EvidenceLinkValidator,
    EvidenceProblemCode,
    ExpectedEvidenceContext,
)
from tests.fakes.evidence_store import FakeEvidenceStore


def _record(**updates: object) -> EvidenceRecord:
    record = EvidenceRecord(
        evidence_id=EvidenceId("ev_1"),
        run_id=RunId("run_1"),
        engagement_id=EngagementId("eng_1"),
        source_type=EvidenceSourceType.TOOL,
        source_id="recon.nmap_scan",
        action_id=ActionId("action_1"),
        invocation_id=InvocationId("inv_1"),
        hypothesis_id=None,
        content_type="application/json",
        artifact_ref=None,
        digest="digest",
        created_at=datetime.now(timezone.utc),
    )
    return record.model_copy(update=updates)


async def _validator_with(record: EvidenceRecord) -> EvidenceLinkValidator:
    store = FakeEvidenceStore()
    await store.append(record)
    return EvidenceLinkValidator(store)


async def test_missing_evidence_is_explicit() -> None:
    result = await EvidenceLinkValidator(FakeEvidenceStore()).validate(
        EvidenceId("missing"), ExpectedEvidenceContext(run_id=RunId("run_1"))
    )
    assert not result.found
    assert result.checks[0].problem_code is EvidenceProblemCode.EVIDENCE_NOT_FOUND


async def test_requested_context_passes_and_unrequested_fields_are_absent() -> None:
    validator = await _validator_with(_record())
    result = await validator.validate(
        EvidenceId("ev_1"),
        ExpectedEvidenceContext(
            engagement_id=EngagementId("eng_1"),
            run_id=RunId("run_1"),
            action_id=ActionId("action_1"),
            invocation_id=InvocationId("inv_1"),
        ),
    )
    assert result.passed
    assert [check.field for check in result.checks] == [
        "engagement_id", "run_id", "action_id", "invocation_id"
    ]


async def test_context_mismatches_are_reported_per_field() -> None:
    validator = await _validator_with(_record())
    result = await validator.validate(
        EvidenceId("ev_1"),
        ExpectedEvidenceContext(
            engagement_id=EngagementId("other_eng"),
            run_id=RunId("other_run"),
            action_id=ActionId("other_action"),
            invocation_id=InvocationId("other_invocation"),
        ),
    )
    assert not result.passed
    assert all(check.status is CheckStatus.MISMATCH for check in result.checks)
    assert all(
        check.problem_code is EvidenceProblemCode.CONTEXT_MISMATCH
        for check in result.checks
    )


async def test_unrequested_and_explicit_none_are_distinct() -> None:
    validator = await _validator_with(_record(hypothesis_id=HypothesisId("hyp_1")))
    unrequested = await validator.validate(EvidenceId("ev_1"), ExpectedEvidenceContext())
    expected_none = await validator.validate(
        EvidenceId("ev_1"), ExpectedEvidenceContext(hypothesis_id=None)
    )

    assert unrequested.checks == ()
    assert not unrequested.passed
    assert expected_none.checks[0].status is CheckStatus.MISMATCH


async def test_missing_optional_link_is_not_reported_unless_requested() -> None:
    validator = await _validator_with(_record(hypothesis_id=None))
    result = await validator.validate(
        EvidenceId("ev_1"), ExpectedEvidenceContext(hypothesis_id=HypothesisId("hyp_1"))
    )
    assert result.checks[0].status is CheckStatus.MISSING
    assert result.checks[0].problem_code is EvidenceProblemCode.REQUIRED_LINK_MISSING


async def test_target_structural_identity_and_unavailable_target() -> None:
    target = HostTarget(host="example.test")
    comparable = await _validator_with(_record(metadata={"target": target.model_dump()}))
    passed = await comparable.validate(
        EvidenceId("ev_1"), ExpectedEvidenceContext(target=target)
    )
    assert passed.passed

    unavailable = await _validator_with(_record(metadata={"target": "example.test"}))
    result = await unavailable.validate(
        EvidenceId("ev_1"), ExpectedEvidenceContext(target=target)
    )
    assert result.checks[0].status is CheckStatus.NOT_COMPARABLE


async def test_display_name_alone_does_not_establish_target_identity() -> None:
    validator = await _validator_with(_record(metadata={"target": "example.test"}))
    result = await validator.validate(
        EvidenceId("ev_1"), ExpectedEvidenceContext(target=HostTarget(host="example.test"))
    )
    assert not result.passed
    assert result.checks[0].problem_code is EvidenceProblemCode.NOT_COMPARABLE


async def test_blocked_evidence_is_valid_but_does_not_satisfy_execution_request() -> None:
    blocked = _record(
        source_type=EvidenceSourceType.SYSTEM,
        source_id="control_plane",
        invocation_id=None,
        artifact_ref=None,
        metadata={"record_kind": "blocked_action"},
    )
    validator = await _validator_with(blocked)

    normal = await validator.validate(
        EvidenceId("ev_1"), ExpectedEvidenceContext(action_id=ActionId("action_1"))
    )
    execution = await validator.validate(
        EvidenceId("ev_1"), ExpectedEvidenceContext(require_execution_evidence=True)
    )
    assert normal.passed
    assert execution.checks[0].problem_code is EvidenceProblemCode.EXECUTION_EVIDENCE_REQUIRED
