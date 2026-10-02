"""Blocked-action Evidence wiring (control-team, track-a).

Verifies DefaultControlPlane records DENY / REQUIRE_APPROVAL decisions as
Evidence via the injected EvidenceService, never records ALLOW, and that
an Evidence-store failure never changes the verdict or blocks the return
(docs/control/blocked-action-evidence.md §10.2).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import pytest

from redteam.control.audit import AuditEntry, AuditLog
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
from tests.fakes.scope_denials import (
    PERSISTENCE_CASES,
    SCOPE_DENY_CASES,
    ScopeDenyCase,
    assert_scope_audit,
    assert_scope_evidence,
)


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
    def __init__(self, audit: AuditLog) -> None:
        super().__init__()
        self.audit = audit
        self.audit_at_append: list[AuditEntry] = []

    async def append(self, evidence: EvidenceRecord) -> None:
        self.audit_at_append = await self.audit.entries_for_run(evidence.run_id)
        raise RuntimeError("synthetic evidence write failure")


@pytest.mark.parametrize("case", PERSISTENCE_CASES, ids=lambda case: case.name)
async def test_evidence_failure_does_not_break_the_decision(
    case: ScopeDenyCase, caplog: pytest.LogCaptureFixture
) -> None:
    engagement = _engagement().model_copy(update={"scope": case.scope})
    proposal = _proposal().model_copy(update={"target": case.target})
    audit = AuditLog()
    store = _ExplodingStore(audit)
    plane = DefaultControlPlane(
        {engagement.engagement_id: engagement},
        audit_log=audit,
        evidence_service=EvidenceService(store),
    )
    with caplog.at_level(logging.ERROR, logger="redteam.control.service"):
        decision = await plane.evaluate(
            proposal, ControlContext(engagement_id=engagement.engagement_id)
        )

    entries = await audit.entries_for_run(proposal.run_id)
    assert len(entries) == 1
    assert_scope_audit(proposal, decision, entries[0], case)
    assert store.audit_at_append == entries
    assert store.records == []
    errors = [record for record in caplog.records if record.name == "redteam.control.service"]
    assert len(errors) == 1
    assert errors[0].getMessage() == "Failed to record blocked action evidence"
    assert errors[0].exc_info is not None
    assert errors[0].action_id == str(proposal.action_id)
    assert errors[0].verdict == "deny"


@pytest.mark.parametrize("case", SCOPE_DENY_CASES, ids=lambda case: case.name)
async def test_scope_deny_records_audit_and_blocked_evidence(case: ScopeDenyCase) -> None:
    engagement = _engagement().model_copy(update={"scope": case.scope})
    proposal = _proposal().model_copy(update={"target": case.target})
    audit = AuditLog()
    store = FakeEvidenceStore()
    plane = DefaultControlPlane(
        {engagement.engagement_id: engagement},
        audit_log=audit,
        evidence_service=EvidenceService(store),
    )
    decision = await plane.evaluate(
        proposal, ControlContext(engagement_id=engagement.engagement_id)
    )
    entries = await audit.entries_for_action(proposal.action_id)
    assert len(entries) == 1
    assert len(store.records) == 1
    assert_scope_audit(proposal, decision, entries[0], case)
    assert_scope_evidence(proposal, decision, store.records[0])
