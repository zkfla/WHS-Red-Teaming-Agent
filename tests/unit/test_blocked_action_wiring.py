"""Blocked-action Evidence wiring (control-team, track-a).

Verifies DefaultControlPlane records DENY / REQUIRE_APPROVAL decisions as
Evidence via the injected EvidenceService, never records ALLOW, and that
an Evidence-store failure never changes the verdict or blocks the return
(docs/control/blocked-action-evidence.md §10.2).
"""

from __future__ import annotations

from datetime import datetime, timezone

from redteam.control.service import DefaultControlPlane
from redteam.core.contracts.action import ActionProposal
from redteam.core.contracts.engagement import (
    ApprovalPolicy,
    Engagement,
    EngagementScope,
    ExecutionBudget,
    RateLimitPolicy,
)
from redteam.core.contracts.evidence import EvidenceRecord
from redteam.core.contracts.policy import ControlContext
from redteam.core.contracts.target import HostTarget
from redteam.core.enums import PolicyVerdict
from redteam.core.ids import ActionId, AgentId, EngagementId, RunId
from redteam.evidence.service import EvidenceService
from tests.fakes.evidence_store import FakeEvidenceStore


def _engagement(*, approval: ApprovalPolicy | None = None) -> Engagement:
    return Engagement(
        engagement_id=EngagementId("eng_bae"),
        name="bae",
        scope=EngagementScope(allowed_hosts=["good.local"]),
        execution_budget=ExecutionBudget(max_actions=5),
        rate_limit_policy=RateLimitPolicy(max_actions_per_minute=30),
        approval_policy=approval or ApprovalPolicy(),
        created_at=datetime.now(timezone.utc),
    )


def _proposal(*, host: str = "good.local") -> ActionProposal:
    return ActionProposal(
        action_id=ActionId("action_bae"),
        run_id=RunId("run_bae"),
        engagement_id=EngagementId("eng_bae"),
        agent_id=AgentId("agent_bae"),
        capability="recon",
        operation="port_scan",
        target=HostTarget(host=host),
        reason="test",
        created_at=datetime.now(timezone.utc),
    )


def _plane(store: FakeEvidenceStore, engagement: Engagement) -> DefaultControlPlane:
    return DefaultControlPlane(
        {engagement.engagement_id: engagement},
        evidence_service=EvidenceService(store),
    )


async def test_denied_action_is_recorded_as_evidence() -> None:
    store = FakeEvidenceStore()
    engagement = _engagement()
    plane = _plane(store, engagement)

    decision = await plane.evaluate(
        _proposal(host="evil.example"),  # out of scope -> DENY
        ControlContext(engagement_id=engagement.engagement_id),
    )

    assert decision.verdict == PolicyVerdict.DENY
    assert len(store.records) == 1
    record = store.records[0]
    assert record.action_id == ActionId("action_bae")
    assert record.invocation_id is None
    assert record.metadata["record_kind"] == "blocked_action"
    assert record.metadata["verdict"] == "deny"


async def test_require_approval_is_recorded_as_evidence() -> None:
    store = FakeEvidenceStore()
    engagement = _engagement(
        approval=ApprovalPolicy(require_approval_for=["recon.port_scan"], auto_approve=False)
    )
    plane = _plane(store, engagement)

    decision = await plane.evaluate(
        _proposal(), ControlContext(engagement_id=engagement.engagement_id)
    )

    assert decision.verdict == PolicyVerdict.REQUIRE_APPROVAL
    assert len(store.records) == 1
    assert store.records[0].metadata["verdict"] == "require_approval"


async def test_allowed_action_is_not_recorded_by_control() -> None:
    store = FakeEvidenceStore()
    engagement = _engagement()
    plane = _plane(store, engagement)

    decision = await plane.evaluate(
        _proposal(), ControlContext(engagement_id=engagement.engagement_id)
    )

    assert decision.verdict == PolicyVerdict.ALLOW
    assert store.records == []  # ALLOW is recorded by Orchestration's verify_node, not here


async def test_no_evidence_service_is_fine() -> None:
    engagement = _engagement()
    plane = DefaultControlPlane({engagement.engagement_id: engagement})  # no evidence_service
    decision = await plane.evaluate(
        _proposal(host="evil.example"),
        ControlContext(engagement_id=engagement.engagement_id),
    )
    assert decision.verdict == PolicyVerdict.DENY  # still works


class _ExplodingStore(FakeEvidenceStore):
    async def append(self, evidence: EvidenceRecord) -> None:
        raise RuntimeError("disk on fire")


async def test_evidence_failure_does_not_break_the_decision() -> None:
    engagement = _engagement()
    plane = _plane(_ExplodingStore(), engagement)

    # Must not raise, and the DENY verdict must be returned unchanged.
    decision = await plane.evaluate(
        _proposal(host="evil.example"),
        ControlContext(engagement_id=engagement.engagement_id),
    )
    assert decision.verdict == PolicyVerdict.DENY
