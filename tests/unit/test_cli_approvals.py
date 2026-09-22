"""`redteam approvals list|approve|deny` — resolves through the durable
Audit trail (`AuditLog.find_approval_request`), the recovery path that
works across a fresh CLI process — see `commands.approvals` module
docstring for why `DefaultControlPlane.resolve_approval`'s in-memory
`_pending_proposals` would not.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path

from redteam.app.cli._shared import EXIT_CONFIG_ERROR, EXIT_FAILURE, EXIT_OK
from redteam.app.cli.commands import approvals as approvals_command
from redteam.control.approval import ApprovalService
from redteam.control.audit import AuditLog
from redteam.control.stores.filesystem import FilesystemApprovalStore, FilesystemAuditStore
from redteam.core.contracts.action import ActionProposal
from redteam.core.contracts.policy import PolicyDecision
from redteam.core.contracts.target import HostTarget
from redteam.core.enums import PolicyVerdict
from redteam.core.ids import ActionId, AgentId, EngagementId, RunId


async def _seed_pending_approval(tmp_path: Path) -> tuple[str, ApprovalService, AuditLog]:
    approval_dir = tmp_path / "approvals"
    audit_dir = tmp_path / "audit"
    approvals = ApprovalService(FilesystemApprovalStore(approval_dir))
    audit = AuditLog(FilesystemAuditStore(audit_dir))

    proposal = ActionProposal(
        action_id=ActionId("action_1"),
        run_id=RunId("run_1"),
        engagement_id=EngagementId("eng_1"),
        agent_id=AgentId("agent_1"),
        capability="web",
        operation="file_upload",
        target=HostTarget(host="a.local"),
        reason="test upload",
        created_at=datetime.now(timezone.utc),
    )
    approval = await approvals.request(proposal.action_id)
    decision = PolicyDecision(
        action_id=proposal.action_id,
        verdict=PolicyVerdict.REQUIRE_APPROVAL,
        reasons=["capability 'web' requires approval"],
        matched_rules=["web"],
        approval_id=approval.approval_id,
        evaluated_at=datetime.now(timezone.utc),
    )
    await audit.record(proposal, decision)
    return str(approval.approval_id), approvals, audit


def _args(approval_id: str, approval_dir: Path, audit_dir: Path, resolved_by=None):  # noqa: ANN001, ANN202
    return argparse.Namespace(
        approval_id=approval_id,
        approval_dir=str(approval_dir),
        audit_dir=str(audit_dir),
        resolved_by=resolved_by,
    )


async def test_list_pending(tmp_path: Path, capsys) -> None:
    approval_id, _, _ = await _seed_pending_approval(tmp_path)
    args = argparse.Namespace(
        run_id=None, approval_dir=str(tmp_path / "approvals"), json=False
    )
    exit_code = await approvals_command.run_list(args)
    assert exit_code == EXIT_OK
    assert approval_id in capsys.readouterr().out


async def test_approve_shows_detail_and_resolves(tmp_path: Path, capsys) -> None:
    approval_id, approvals, _ = await _seed_pending_approval(tmp_path)
    args = _args(approval_id, tmp_path / "approvals", tmp_path / "audit", resolved_by="tester")

    exit_code = await approvals_command.run_approve(args)

    assert exit_code == EXIT_OK
    out = capsys.readouterr().out
    assert "Action: web.file_upload" in out
    assert "-> approved (by tester)" in out
    resolved = await approvals.get(approval_id)
    assert resolved is not None
    assert resolved.status.value == "approved"


async def test_deny_resolves_as_rejected(tmp_path: Path) -> None:
    approval_id, approvals, _ = await _seed_pending_approval(tmp_path)
    args = _args(approval_id, tmp_path / "approvals", tmp_path / "audit", resolved_by="tester")

    exit_code = await approvals_command.run_deny(args)

    assert exit_code == EXIT_OK
    resolved = await approvals.get(approval_id)
    assert resolved is not None
    assert resolved.status.value == "rejected"


async def test_duplicate_resolve_is_reported_not_crashed(tmp_path: Path, capsys) -> None:
    approval_id, _, _ = await _seed_pending_approval(tmp_path)
    args = _args(approval_id, tmp_path / "approvals", tmp_path / "audit", resolved_by="tester")
    first = await approvals_command.run_approve(args)
    assert first == EXIT_OK

    second = await approvals_command.run_approve(args)

    assert second == EXIT_CONFIG_ERROR
    assert "already" in capsys.readouterr().err


async def test_unknown_approval_id_is_reported_not_crashed(tmp_path: Path, capsys) -> None:
    args = _args("appr_does_not_exist", tmp_path / "approvals", tmp_path / "audit")
    exit_code = await approvals_command.run_approve(args)
    assert exit_code == EXIT_CONFIG_ERROR
    assert "no such approval" in capsys.readouterr().err


async def test_approve_without_audit_entry_fails_explicitly(tmp_path: Path, capsys) -> None:
    """An Approval that somehow has no matching policy-decision audit
    entry must fail loudly, not silently resolve without context."""
    approval_dir = tmp_path / "approvals"
    approvals = ApprovalService(FilesystemApprovalStore(approval_dir))
    approval = await approvals.request(ActionId("action_orphan"))

    args = _args(str(approval.approval_id), approval_dir, tmp_path / "audit")
    exit_code = await approvals_command.run_approve(args)

    assert exit_code == EXIT_FAILURE
    assert "no policy-decision audit entry" in capsys.readouterr().err
