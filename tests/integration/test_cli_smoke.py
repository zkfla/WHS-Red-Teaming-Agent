"""CLI persistence and mandatory-model contracts through real commands."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

from redteam.app.bootstrap import run_engagement
from redteam.app.cli.commands import run as run_command
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
from tests.integration.test_end_to_end import _world

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
        out, err = await asyncio.wait_for(proc.communicate(), timeout=20)
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
    assert proc.returncode is not None
    return proc.returncode, out.decode(), err.decode()


async def test_init_validate_and_persisted_status_findings_report(tmp_path, monkeypatch):
    code, out, err = await _run_cli("init", "--output", "engagement.yaml", cwd=tmp_path)
    assert code == 0, err
    assert "Created engagement config" in out
    code, out, err = await _run_cli("validate", "engagement.yaml", cwd=tmp_path)
    assert code == 0, err
    assert "Configuration valid." in out
    monkeypatch.chdir(tmp_path)
    engagement, deps, gateway = _world()
    final = await run_engagement(engagement, deps)
    assert len(gateway.calls) == 1
    code, out, err = await _run_cli("status", str(final.run_id), cwd=tmp_path)
    assert code == 0, err
    assert "FINISHED" in out
    code, _, err = await _run_cli("findings", "list", str(final.run_id), cwd=tmp_path)
    assert code == 0, err
    code, out, err = await _run_cli("report", "generate", str(final.run_id), cwd=tmp_path)
    assert code == 0, err
    assert "Report generated" in out
    assert (tmp_path / "reports" / str(final.run_id) / "report.md").exists()


@pytest.mark.parametrize("json_mode", [False, True])
async def test_fake_provider_is_rejected_before_run(tmp_path, json_mode):
    (tmp_path / "engagement.yaml").write_text(_ENGAGEMENT, encoding="utf-8")
    options = ["--json"] if json_mode else []
    code, out, err = await _run_cli(
        "run", "engagement.yaml", "--non-interactive", *options, cwd=tmp_path
    )
    assert code != 0
    assert "fake" in err and "Traceback" not in err
    assert "ToolExecutionStarted" not in out
    assert not (tmp_path / "data" / "reports").exists()


async def test_json_command_streams_actual_events(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("REDTEAM_LLM_PROVIDER", "litellm")
    monkeypatch.delenv("REDTEAM_MCP_SERVERS", raising=False)
    path = tmp_path / "engagement.yaml"
    path.write_text(_ENGAGEMENT, encoding="utf-8")
    engagement, deps, gateway = _world()

    async def build(config, **kwargs):
        return engagement, deps

    monkeypatch.setattr(run_command, "build_application", build)
    code = await run_command.run(
        argparse.Namespace(
            engagement_path=str(path),
            non_interactive=True,
            json=True,
            max_steps=None,
            log_level=None,
            objective=None,
        )
    )
    assert code == 0 and len(gateway.calls) == 1
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert events and events[-1]["event_type"] == "RunFinished"
    assert any(event["event_type"] == "ToolExecutionFinished" for event in events)


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
