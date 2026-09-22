"""Contract Conversion Function tests (instruction.md section 23).

Only mechanical conversions are exercised here — no policy/business
logic (that's Control Team's ``redteam.control`` and covered by
``tests/unit/test_scope.py`` / the integration test instead).
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from redteam.core.contracts.action import ActionProposal
from redteam.core.contracts.policy import PolicyDecision
from redteam.core.contracts.target import HostTarget
from redteam.core.contracts.tool import ToolResult
from redteam.core.convert import (
    NonAllowDecisionError,
    build_tool_invocation,
    tool_result_to_evidence,
)
from redteam.core.enums import PolicyVerdict
from redteam.core.ids import ActionId, AgentId, EngagementId, InvocationId, RunId, ToolId


def _proposal() -> ActionProposal:
    return ActionProposal(
        action_id=ActionId("action_1"),
        run_id=RunId("run_1"),
        engagement_id=EngagementId("eng_1"),
        agent_id=AgentId("agent_1"),
        capability="recon.example",
        operation="ping",
        target=HostTarget(host="a.local"),
        reason="test",
        created_at=datetime.now(timezone.utc),
    )


def _decision(verdict: PolicyVerdict, action_id: ActionId | None = None) -> PolicyDecision:
    return PolicyDecision(
        action_id=action_id or ActionId("action_1"),
        verdict=verdict,
        evaluated_at=datetime.now(timezone.utc),
    )


def test_build_tool_invocation_from_allow_decision() -> None:
    proposal = _proposal()
    decision = _decision(PolicyVerdict.ALLOW)

    invocation = build_tool_invocation(proposal, decision, tool_id=ToolId("example.ping"))

    assert invocation.action_id == proposal.action_id
    assert invocation.run_id == proposal.run_id
    assert invocation.tool_id == ToolId("example.ping")
    assert invocation.execution_context.engagement_id == proposal.engagement_id
    assert invocation.execution_context.agent_id == proposal.agent_id


@pytest.mark.parametrize("verdict", [PolicyVerdict.DENY, PolicyVerdict.REQUIRE_APPROVAL])
def test_build_tool_invocation_refuses_non_allow_decision(verdict: PolicyVerdict) -> None:
    proposal = _proposal()
    decision = _decision(verdict)

    with pytest.raises(NonAllowDecisionError):
        build_tool_invocation(proposal, decision, tool_id=ToolId("example.ping"))


def test_build_tool_invocation_refuses_mismatched_action_id() -> None:
    proposal = _proposal()
    decision = _decision(PolicyVerdict.ALLOW, action_id=ActionId("some_other_action"))

    with pytest.raises(ValueError):
        build_tool_invocation(proposal, decision, tool_id=ToolId("example.ping"))


def test_tool_result_to_evidence_keeps_correlation() -> None:
    result = ToolResult(
        invocation_id=InvocationId("inv_1"),
        action_id=ActionId("action_1"),
        tool_id=ToolId("example.ping"),
        success=True,
        output={"ok": True},
        started_at=datetime.now(timezone.utc),
        finished_at=datetime.now(timezone.utc),
    )

    evidence = tool_result_to_evidence(
        result, run_id=RunId("run_1"), engagement_id=EngagementId("eng_1")
    )

    assert evidence.run_id == RunId("run_1")
    assert evidence.engagement_id == EngagementId("eng_1")
    assert evidence.action_id == ActionId("action_1")
    assert evidence.invocation_id == InvocationId("inv_1")
    assert evidence.digest
