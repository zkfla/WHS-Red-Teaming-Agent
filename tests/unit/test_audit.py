"""Audit Test: policy decisions and approval resolutions are persisted,
survive a restart (new AuditLog/store instance), and never leak
sensitive-looking argument values verbatim.
"""

from __future__ import annotations

import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from redteam.control.approval import ApprovalService
from redteam.control.audit import AuditLog, redact_arguments
from redteam.control.service import DefaultControlPlane
from redteam.control.stores.filesystem import FilesystemApprovalStore, FilesystemAuditStore
from redteam.core.contracts.action import ActionProposal
from redteam.core.contracts.engagement import (
    ApprovalPolicy,
    Engagement,
    EngagementScope,
    ExecutionBudget,
    RateLimitPolicy,
)
from redteam.core.contracts.policy import ControlContext, PolicyDecision
from redteam.core.contracts.target import HostTarget
from redteam.core.enums import ApprovalStatus, PolicyVerdict
from redteam.core.ids import ActionId, AgentId, EngagementId, RunId


def _proposal(capability: str, operation: str) -> ActionProposal:
    """Builds an ActionProposal using only canonical capability/operation
    values (docs/contracts/actions.md): capability is one of the fixed
    namespaces (recon/web/mobile/analysis), operation is one of the
    registered canonical action names. Callers in this file must stick
    to that vocabulary rather than inventing new strings.
    """
    return ActionProposal(
        action_id=ActionId("action_1"),
        run_id=RunId("run_1"),
        engagement_id=EngagementId("eng_1"),
        agent_id=AgentId("agent_1"),
        capability=capability,
        operation=operation,
        target=HostTarget(host="good.local"),
        arguments={"session_token": "super-secret-value", "note": "not sensitive"},
        reason="test",
        created_at=datetime.now(timezone.utc),
    )


def _engagement(*, require_approval_for: list[str] | None = None) -> Engagement:
    return Engagement(
        engagement_id=EngagementId("eng_1"),
        name="test engagement",
        scope=EngagementScope(allowed_hosts=["good.local"]),
        execution_budget=ExecutionBudget(max_actions=5),
        rate_limit_policy=RateLimitPolicy(max_actions_per_minute=30),
        approval_policy=ApprovalPolicy(require_approval_for=require_approval_for or []),
        created_at=datetime.now(timezone.utc),
    )


def test_redact_arguments_masks_sensitive_keys_only() -> None:
    redacted = redact_arguments(
        {"session_token": "abc123", "note": "keep me", "nested": {"account_number": "123"}}
    )
    assert redacted["session_token"] == "***REDACTED***"
    assert redacted["note"] == "keep me"
    assert redacted["nested"]["account_number"] == "***REDACTED***"


async def test_filesystem_audit_store_persists_across_instances(tmp_path: Path) -> None:
    """The whole point of a durable sink: a fresh AuditLog/store pointed
    at the same directory sees entries written by a previous one."""
    store_a = FilesystemAuditStore(tmp_path)
    log_a = AuditLog(store_a)
    proposal = _proposal("recon", "port_scan")

    decision = PolicyDecision(
        action_id=proposal.action_id,
        verdict=PolicyVerdict.ALLOW,
        evaluated_at=datetime.now(timezone.utc),
    )
    await log_a.record(proposal, decision)

    # Simulate a restart: brand-new AuditLog/store pointed at the same directory.
    store_b = FilesystemAuditStore(tmp_path)
    log_b = AuditLog(store_b)

    entries = await log_b.entries_for_run(RunId("run_1"))
    assert len(entries) == 1
    assert entries[0].action_id == proposal.action_id
    # The raw session_token must never reach disk.
    assert entries[0].action_summary is not None
    assert entries[0].action_summary["arguments"]["session_token"] == "***REDACTED***"


async def test_audit_redacts_policy_reasons_and_proposal_text_on_disk(tmp_path: Path) -> None:
    log = AuditLog(FilesystemAuditStore(tmp_path))
    proposal = _proposal("recon", "port_scan").model_copy(
        update={
            "reason": "Authorization: Bearer proposal-secret",
            "arguments": {"note": "session_id=argument-secret"},
        }
    )
    decision = PolicyDecision(
        action_id=proposal.action_id,
        verdict=PolicyVerdict.DENY,
        reasons=["Cookie: sid=reason-secret", "api_key=another-secret"],
        matched_rules=["rule: token=rule-secret"],
        evaluated_at=datetime.now(timezone.utc),
    )

    await log.record(proposal, decision)

    stored_text = (tmp_path / "run_1.jsonl").read_text(encoding="utf-8")
    for secret in (
        "proposal-secret",
        "argument-secret",
        "reason-secret",
        "another-secret",
        "rule-secret",
    ):
        assert secret not in stored_text
    stored = (await log.entries_for_run(proposal.run_id))[0]
    assert stored.decision is not None
    assert stored.decision.reasons == ["Cookie: ***REDACTED***", "api_key=***REDACTED***"]
    assert decision.reasons == ["Cookie: sid=reason-secret", "api_key=another-secret"]


async def test_control_plane_records_approval_resolution() -> None:
    """Closes the traceability gap: resolving a pending Approval must
    produce an APPROVAL_RESOLVED audit entry correlated back to the
    original action, not just the REQUIRE_APPROVAL decision.

    Uses "web.auth_attempt" for ApprovalPolicy.require_approval_for --
    control.policy.evaluate_policy matches it against the same
    "<capability>.<operation>" canonical action key used by ROE (unified
    on the dot form per docs/contracts/actions.md).
    """
    engagement = _engagement(require_approval_for=["web.auth_attempt"])
    audit_log = AuditLog()  # in-memory is enough for this test
    control_plane = DefaultControlPlane(
        {engagement.engagement_id: engagement}, audit_log=audit_log
    )

    proposal = _proposal("web", "auth_attempt")
    decision = await control_plane.evaluate(
        proposal, ControlContext(engagement_id=engagement.engagement_id)
    )
    assert decision.verdict == PolicyVerdict.REQUIRE_APPROVAL
    assert decision.approval_id is not None

    await control_plane.resolve_approval(
        decision.approval_id, ApprovalStatus.APPROVED, "operator"
    )

    entries = await audit_log.entries_for_action(proposal.action_id)
    entry_types = [e.entry_type.value for e in entries]
    assert "policy_decision" in entry_types
    assert "approval_resolved" in entry_types

    resolved_entry = next(e for e in entries if e.entry_type.value == "approval_resolved")
    assert resolved_entry.approval is not None
    assert resolved_entry.approval.status == ApprovalStatus.APPROVED


@pytest.mark.parametrize("status", ["approved", "rejected"])
async def test_cli_resolution_records_audit_after_restart(tmp_path: Path, status: str) -> None:
    approval_dir = tmp_path / "approvals"
    audit_dir = tmp_path / "audit"
    approvals = ApprovalService(FilesystemApprovalStore(approval_dir))
    approval = await approvals.request(ActionId("action_1"))
    proposal = _proposal("web", "auth_attempt")
    await AuditLog(FilesystemAuditStore(audit_dir)).record(
        proposal,
        PolicyDecision(
            action_id=proposal.action_id,
            verdict=PolicyVerdict.REQUIRE_APPROVAL,
            approval_id=approval.approval_id,
            evaluated_at=datetime.now(timezone.utc),
        ),
    )

    result = subprocess.run(
        [
            sys.executable, "-m", "redteam.app.cli", "approvals", "resolve",
            str(approval.approval_id), status, "--by", "operator",
            "--dir", str(approval_dir), "--audit-dir", str(audit_dir),
        ],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr

    entries = await AuditLog(FilesystemAuditStore(audit_dir)).entries_for_run(proposal.run_id)
    assert len(entries) == 2
    resolved = entries[1]
    assert resolved.entry_type.value == "approval_resolved"
    assert resolved.engagement_id == proposal.engagement_id
    assert resolved.action_id == proposal.action_id
    assert resolved.approval is not None
    assert resolved.approval.approval_id == approval.approval_id
    assert resolved.approval.status == ApprovalStatus(status)

    repeated = subprocess.run(result.args, capture_output=True, text=True, check=False)
    assert repeated.returncode != 0
    entries_after_retry = await AuditLog(FilesystemAuditStore(audit_dir)).entries_for_run(
        proposal.run_id
    )
    assert len(entries_after_retry) == 2


async def test_cli_does_not_resolve_without_policy_audit(tmp_path: Path) -> None:
    approval_dir = tmp_path / "approvals"
    approval = await ApprovalService(FilesystemApprovalStore(approval_dir)).request(
        ActionId("action_1")
    )

    result = subprocess.run(
        [
            sys.executable, "-m", "redteam.app.cli", "approvals", "resolve",
            str(approval.approval_id), "approved", "--by", "operator",
            "--dir", str(approval_dir), "--audit-dir", str(tmp_path / "audit"),
        ],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode != 0
    stored = await ApprovalService(FilesystemApprovalStore(approval_dir)).get(
        approval.approval_id
    )
    assert stored is not None
    assert stored.status == ApprovalStatus.PENDING
