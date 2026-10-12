"""CLI persistence and mandatory-model contracts through real commands (native loop)."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

from redteam.control.approval import ApprovalService
from redteam.control.audit import AuditLog
from redteam.control.service import DefaultControlPlane
from redteam.control.stores.filesystem import FilesystemApprovalStore, FilesystemAuditStore
from redteam.core.contracts.action import ActionProposal
from redteam.core.contracts.engagement import ApprovalPolicy
from redteam.core.contracts.policy import ControlContext
from redteam.core.contracts.target import HostTarget
from redteam.core.enums import ApprovalStatus, PolicyVerdict
from redteam.core.ids import ActionId, AgentId, RunId
from tests.integration import _engine_llm_harness as h


@pytest.fixture(autouse=True)
def explicit_legacy_runtime(monkeypatch):
    """These fixtures exercise the retained legacy CLI/schema, explicitly selected."""
    monkeypatch.setenv("REDTEAM_RUNTIME_BACKEND", "legacy")

_ENGAGEMENT = """
name: CLI Smoke
scope:
  allowed_hosts: [host-01]
execution_budget:
  max_actions: 5
rate_limit_policy:
  max_actions_per_minute: 30
approval_policy:
  require_approval_for: []
"""
















async def _run_cli(*args: str, cwd: Path) -> tuple[int, str, str]:
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith("REDTEAM_")
    }
    environment["REDTEAM_LLM_PROVIDER"] = "fake"
    environment["REDTEAM_RUNTIME_BACKEND"] = "legacy"
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "redteam.app.cli",
        *args,
        cwd=str(cwd),
        env=environment,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=120)
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
    assert proc.returncode is not None
    return proc.returncode, out.decode(), err.decode()








@pytest.mark.parametrize(
    "command,status",
    [
        ("approve", ApprovalStatus.APPROVED),
        ("deny", ApprovalStatus.REJECTED),
    ],
)
async def test_approval_command_resolves_durable_policy_request(tmp_path, command, status):
    engagement = h.make_engagement().model_copy(
        update={
            "approval_policy": ApprovalPolicy(require_approval_for=["recon"]),
        }
    )
    approvals = ApprovalService(FilesystemApprovalStore(tmp_path / "data" / "approvals"))
    control = DefaultControlPlane(
        {engagement.engagement_id: engagement},
        approvals=approvals,
        audit_log=AuditLog(FilesystemAuditStore(tmp_path / "data" / "audit")),
    )
    proposal = ActionProposal(
        action_id=ActionId("action-cli"),
        run_id=RunId("run-cli"),
        engagement_id=engagement.engagement_id,
        agent_id=AgentId(h.RECON_AGENT),
        capability="recon",
        operation="service_discovery",
        target=HostTarget(host=h.HOST),
        arguments={},
        reason="inspect declared target",
        created_at=h.utcnow(),
    )
    decision = await control.evaluate(
        proposal, ControlContext(engagement_id=engagement.engagement_id)
    )
    assert decision.verdict is PolicyVerdict.REQUIRE_APPROVAL
    code, out, err = await _run_cli("approvals", command, str(decision.approval_id), cwd=tmp_path)
    assert code == 0, err
    assert status.value in out
    saved = await approvals.get(decision.approval_id)
    assert saved is not None and saved.status is status
    code, _, _ = await _run_cli("approvals", command, str(decision.approval_id), cwd=tmp_path)
    assert code != 0


async def test_no_subcommand_dispatches_to_interactive_not_run(tmp_path):
    code, out, _ = await _run_cli(cwd=tmp_path)
    assert "REDTEAM" in out
    assert "no command handler registered" not in out
    assert code == 2
