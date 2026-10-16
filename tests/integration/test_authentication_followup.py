"""Authentication follow-ups are bounded, policy-gated and observation-only.

Native-loop notes: every request below comes from explicit scripted
complete_native calls through the production InvestigationEngine path.
The scripted chain is 1 anonymous probe + 1 selected session check, then
the registered local authentication.assess over the two stored evidence
IDs, then (deny only) one explicit graph.record security claim. The model
holds afterwards; nothing runs on a rule fallback.

Retired deterministic items (with replacement):
- legacy complete_structured strategy scripting (path drafts, path reviews
  and next_action intents) -> complete_native tool calls plus the genuine
  analysis/task/goal completion reviews only.
- denied-session violation promotion via the legacy verify branch
  (deny + 200 -> access_control_violation with a security_check event):
  the local assess step stays observation-only, so AUTHENTICATION_CHECKED
  now carries assessment_kind observation even for deny + 200, and the
  verified high-severity Finding, the confirmed hypothesis and their
  linkage come from the explicit graph.record claim instead. The
  observation-only chain above stays finding-free.
- deferred/tool-missing branches via auto-candidates ->
  covered as policy/availability outcomes in test_session_followup_deny_blocks
  and the DENY control-plane cases in test_agent_loop.py.
"""

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
    AuthenticationCheck,
    assess_authentication,
    comparison_outcome,
)
from tests.fakes import FakeEvidenceStore

HOST_NODE = "host:app.local"
WEB_TOOL = "web.http_request"




















@pytest.mark.parametrize(
    "status,expected",
    [(200, "session_access_observed"), (401, "session_not_accepted"), (403, "access_denied")],
)
def test_comparison_outcome_matrix(status: int, expected: str) -> None:
    assert comparison_outcome(status, "unspecified") == expected
    assert comparison_outcome(None, "unspecified") == "request_failed_or_uninterpretable"
    assert comparison_outcome(200, "deny") == "access_control_violation"


def _unit_evidence(
    store: FakeEvidenceStore,
    *,
    evidence_id: str,
    run_id: str,
    engagement_id: str,
    http_status: int | None,
    session_ref: str | None,
) -> EvidenceRecord:
    now = datetime.now(timezone.utc)
    return EvidenceRecord(
        evidence_id=EvidenceId(evidence_id),
        run_id=RunId(run_id),
        engagement_id=EngagementId(engagement_id),
        source_type=EvidenceSourceType.TOOL,
        source_id="web.http_request",
        action_id=ActionId("action_unit"),
        invocation_id=InvocationId("inv_unit"),
        content_type="application/json",
        digest="00" * 32,
        summary="unit baseline",
        created_at=now,
        metadata={
            "success": True,
            "target_node_id": HOST_NODE,
            "tool_result": {
                "success": True,
                "output": {"status_code": http_status, "actual_session_ref": session_ref},
            },
            "http_observation": {
                "kind": "http_response",
                "method": "GET",
                "path": "/",
                "target_ref": "app",
                "session_ref": session_ref,
                "status_code": http_status,
            },
        },
    )


@pytest.mark.parametrize("mismatch", ["missing", "path", "target_ref", "session_ref", "method"])
async def test_mismatched_baseline_or_request_cannot_confirm(mismatch: str) -> None:
    store = FakeEvidenceStore()
    now = datetime.now(timezone.utc)
    baseline = _unit_evidence(
        store, evidence_id="ev-base", run_id="run_unit", engagement_id="eng_unit",
        http_status=401, session_ref=None,
    )
    await store.append(baseline)
    current = _unit_evidence(
        store, evidence_id="ev-now", run_id="run_unit", engagement_id="eng_unit",
        http_status=200, session_ref="alice",
    )
    arguments: dict[str, Any] = {
        "target_ref": "app",
        "path": "/",
        "method": "GET",
        "session_ref": "alice",
    }
    baseline_ref = "ev-base"
    if mismatch == "missing":
        baseline_ref = "ev-gone"
    elif mismatch == "method":
        arguments = {**arguments, "method": "POST"}
    elif mismatch == "path":
        arguments = {**arguments, "path": "/different"}
    elif mismatch == "target_ref":
        arguments = {**arguments, "target_ref": "/different"}
    elif mismatch == "session_ref":
        arguments = {**arguments, "session_ref": "/different"}
    proposal = ActionProposal(
        action_id=ActionId("action_unit"),
        run_id=RunId("run_unit"),
        engagement_id=EngagementId("eng_unit"),
        agent_id=AgentId("web-api-agent"),
        capability="web",
        operation="http_request",
        target=HostTarget(host="app.local"),
        arguments=arguments,
        reason="unit",
        created_at=now,
    )
    result = ToolResult(
        invocation_id=InvocationId("inv_unit"),
        action_id=ActionId("action_unit"),
        tool_id=ToolId("web.http_request"),
        success=True,
        output={"status_code": 200, "actual_session_ref": "alice"},
        started_at=now,
        finished_at=now,
    )
    assessment = await assess_authentication(
        AuthenticationCheck(baseline_evidence_id=EvidenceId(baseline_ref), session_ref="alice"),
        proposal,
        result,
        current,
        store,
    )
    assert assessment.hypothesis_status == "inconclusive"
    assert assessment.outcome == "baseline_evidence_unavailable_or_mismatched"
    assert assessment.evidence_ids == ["ev-now"]
