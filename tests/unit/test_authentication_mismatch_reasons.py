"""Fixed mismatch reasons for authentication baseline comparisons."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest

from redteam.core.contracts.action import ActionProposal
from redteam.core.contracts.evidence import EvidenceRecord
from redteam.core.contracts.target import HostTarget
from redteam.core.contracts.tool import ToolResult
from redteam.core.enums import EvidenceSourceType
from redteam.core.ids import (
    ActionId,
    AgentId,
    EngagementId,
    EvidenceId,
    InvocationId,
    RunId,
    ToolId,
)
from redteam.orchestration.authentication import (
    AuthenticationAssessment,
    AuthenticationCheck,
    assess_authentication,
)
from tests.fakes.evidence_store import FakeEvidenceStore

RUN = "run_unit"
ENGAGEMENT = "eng_unit"
WEB_TOOL = "web.http_request"
NODE = "host:app.local"
BASE_ARGS: dict[str, Any] = {
    "target_ref": "app",
    "path": "/",
    "method": "GET",
    "session_ref": "alice",
}
BASE_OBSERVATION: dict[str, Any] = {
    "kind": "http_response",
    "method": "GET",
    "path": "/",
    "target_ref": "app",
    "session_ref": None,
    "status_code": 401,
}

CASES: list[Any] = [
    pytest.param("baseline_missing", {"store_baseline": False}, id="baseline_missing"),
    pytest.param(
        "baseline_scope_mismatch",
        {"baseline_engagement": "eng_other"},
        id="baseline_scope_mismatch",
    ),
    pytest.param(
        "baseline_source_mismatch",
        {"baseline_source": "web.other"},
        id="baseline_source_mismatch",
    ),
    pytest.param(
        "baseline_not_successful",
        {"baseline_meta": {"success": False}},
        id="baseline_not_successful",
    ),
    pytest.param(
        "request_method_mismatch",
        {"args": {"method": "POST"}},
        id="request_method_mismatch",
    ),
    pytest.param(
        "request_session_mismatch",
        {"args": {"session_ref": "bob"}},
        id="request_session_mismatch",
    ),
    pytest.param("target_mismatch", {"current_node": "host:other"}, id="target_mismatch"),
    pytest.param(
        "baseline_observation_missing",
        {"drop_observation": True},
        id="baseline_observation_missing",
    ),
    pytest.param(
        "baseline_status_mismatch",
        {"observation": {"status_code": 403}},
        id="baseline_status_mismatch",
    ),
    pytest.param(
        "baseline_session_mismatch",
        {"observation": {"session_ref": "alice"}},
        id="baseline_session_mismatch",
    ),
    pytest.param(
        "baseline_method_mismatch",
        {"observation": {"method": "POST"}},
        id="baseline_method_mismatch",
    ),
    pytest.param(
        "baseline_path_mismatch",
        {"observation": {"path": "/other"}},
        id="baseline_path_mismatch",
    ),
    pytest.param(
        "baseline_target_ref_mismatch",
        {"observation": {"target_ref": "other"}},
        id="baseline_target_ref_mismatch",
    ),
    pytest.param(
        "baseline_actual_session_receipt_mismatch",
        {"baseline_output": {"status_code": 401}},
        id="baseline_receipt_echo_only",
    ),
    pytest.param(
        "baseline_actual_session_receipt_mismatch",
        {"baseline_output": {"status_code": 401, "actual_session_ref": "alice"}},
        id="baseline_receipt_wrong_session",
    ),
    pytest.param(
        "baseline_request_headers_ambiguous",
        {"baseline_args": {"request_headers": {"X-Api-Key": "fixture-key"}}},
        id="baseline_custom_headers",
    ),
    pytest.param(
        "request_actual_session_receipt_mismatch",
        {"current_output": {"status_code": 200}},
        id="current_receipt_missing",
    ),
    pytest.param(
        "request_actual_session_receipt_mismatch",
        {"current_output": {"status_code": 200, "actual_session_ref": "mallory"}},
        id="current_receipt_wrong_session",
    ),
]


def _record(
    evidence_id: str,
    *,
    metadata: dict[str, Any],
    engagement_id: str = ENGAGEMENT,
    source_id: str = WEB_TOOL,
) -> EvidenceRecord:
    return EvidenceRecord(
        evidence_id=EvidenceId(evidence_id),
        run_id=RunId(RUN),
        engagement_id=EngagementId(engagement_id),
        source_type=EvidenceSourceType.TOOL,
        source_id=source_id,
        action_id=ActionId("action_unit"),
        invocation_id=InvocationId("inv_unit"),
        content_type="application/json",
        digest="00" * 32,
        summary="unit evidence",
        created_at=datetime.now(timezone.utc),
        metadata=metadata,
    )


async def _assess(case: dict[str, Any], *, status: int | None = 200) -> AuthenticationAssessment:
    store = FakeEvidenceStore()
    now = datetime.now(timezone.utc)
    baseline_meta: dict[str, Any] = {
        "success": True,
        "target_node_id": NODE,
        **case.get("baseline_meta", {}),
    }
    if not case.get("drop_observation"):
        baseline_meta["http_observation"] = {**BASE_OBSERVATION, **case.get("observation", {})}
    baseline_meta["tool_result"] = {
        "success": True,
        "output": case.get(
            "baseline_output", {"status_code": 401, "actual_session_ref": None}
        ),
    }
    baseline_meta["proposal"] = {"arguments": case.get("baseline_args", {})}
    baseline = _record(
        "ev-base",
        metadata=baseline_meta,
        engagement_id=case.get("baseline_engagement", ENGAGEMENT),
        source_id=case.get("baseline_source", WEB_TOOL),
    )
    if case.get("store_baseline", True):
        await store.append(baseline)
    current = _record(
        "ev-now",
        metadata={"success": True, "target_node_id": case.get("current_node", NODE)},
    )
    await store.append(current)
    proposal = ActionProposal(
        action_id=ActionId("action_unit"),
        run_id=RunId(RUN),
        engagement_id=EngagementId(ENGAGEMENT),
        agent_id=AgentId("web-api-agent"),
        capability="web",
        operation="http_request",
        target=HostTarget(host="app.local"),
        arguments={**BASE_ARGS, **case.get("args", {})},
        reason="unit",
        created_at=now,
    )
    output: dict[str, Any] = (
        case.get("current_output", {"status_code": status, "actual_session_ref": "alice"})
        if status is not None else {}
    )
    result = ToolResult(
        invocation_id=InvocationId("inv_unit"),
        action_id=ActionId("action_unit"),
        tool_id=ToolId("web.http_request"),
        success=True,
        output=output,
        started_at=now,
        finished_at=now,
    )
    return await assess_authentication(
        AuthenticationCheck(baseline_evidence_id=EvidenceId("ev-base"), session_ref="alice"),
        proposal,
        result,
        current,
        store,
    )


@pytest.mark.parametrize(("reason", "case"), CASES)
async def test_each_failed_baseline_condition_names_its_fixed_reason(
    reason: str,
    case: dict[str, Any],
) -> None:
    assessment = await _assess(case)
    assert assessment.outcome == "baseline_evidence_unavailable_or_mismatched"
    assert assessment.hypothesis_status == "inconclusive"
    assert assessment.evidence_ids == ["ev-now"]
    assert assessment.mismatch_reason == reason
    assert assessment.anonymous_status is None
    assert assessment.session_status is None


@pytest.mark.parametrize(
    ("status", "outcome", "hypothesis_status"),
    [
        (200, "session_access_observed", "confirmed"),
        (401, "session_not_accepted", "rejected"),
        (403, "access_denied", "rejected"),
        (302, "inconclusive_response", "inconclusive"),
    ],
)
async def test_valid_401_baseline_keeps_outcome_and_has_no_reason(
    status: int,
    outcome: str,
    hypothesis_status: str,
) -> None:
    assessment = await _assess({}, status=status)
    assert assessment.outcome == outcome
    assert assessment.hypothesis_status == hypothesis_status
    assert assessment.mismatch_reason is None
    assert assessment.evidence_ids == ["ev-base", "ev-now"]
    assert assessment.anonymous_status == 401
    assert assessment.session_status == status


async def test_valid_baseline_with_unusable_current_response_is_not_a_baseline_mismatch() -> None:
    assessment = await _assess({}, status=None)
    assert assessment.outcome == "request_failed_or_uninterpretable"
    assert assessment.hypothesis_status == "inconclusive"
    assert assessment.mismatch_reason is None
    assert assessment.evidence_ids == ["ev-base", "ev-now"]


async def test_the_first_failed_condition_is_the_reported_reason() -> None:
    assessment = await _assess({"baseline_engagement": "eng_other", "args": {"method": "POST"}})
    assert assessment.mismatch_reason == "baseline_scope_mismatch"


@pytest.mark.parametrize(
    "case",
    [
        pytest.param({"args": {"session_ref": "sk-live-SECRET-VALUE-123"}}, id="session"),
        pytest.param({"observation": {"path": "/sk-live-SECRET-VALUE-123"}}, id="path"),
        pytest.param(
            {"baseline_args": {"request_headers": {"X-Api-Key": "sk-live-SECRET-VALUE-123"}}},
            id="custom-header",
        ),
    ],
)
async def test_provided_values_are_never_echoed_in_the_assessment(case: dict[str, Any]) -> None:
    assessment = await _assess(case)
    dumped = assessment.model_dump_json()
    assert "SECRET-VALUE" not in dumped
    assert "sk-live" not in dumped
    assert assessment.mismatch_reason is not None
