"""CLI smoke tests: real subprocesses, real `build_application()` path,
the harmless in-process `ExampleMCPAdapter` as the "MCP ping server"
(section 27 -- no real exploit tooling is exercised by this CLI's own
tests). Each test runs in its own `tmp_path` so the filesystem-backed
Approval/Audit/Evidence/report stores never collide across tests.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.asyncio

_OPEN_ENGAGEMENT = """
name: "CLI Smoke"
scope:
  allowed_hosts: ["a.local"]
execution_budget:
  max_actions: 5
rate_limit_policy:
  max_actions_per_minute: 30
approval_policy:
  require_approval_for: []
"""

_APPROVAL_ENGAGEMENT = """
name: "CLI Smoke Approval"
scope:
  allowed_hosts: ["a.local"]
execution_budget:
  max_actions: 5
rate_limit_policy:
  max_actions_per_minute: 30
approval_policy:
  require_approval_for: ["recon"]
"""

_DENY_ENGAGEMENT = """
name: "CLI Smoke Deny"
scope:
  allowed_hosts: ["a.local"]
rules_of_engagement:
  - name: "no-recon"
    denied_capabilities: ["recon"]
execution_budget:
  max_actions: 5
rate_limit_policy:
  max_actions_per_minute: 30
approval_policy:
  require_approval_for: []
"""


async def _run_cli(*args: str, cwd: Path) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "redteam.app.cli",
        *args,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
    assert proc.returncode is not None
    return proc.returncode, stdout.decode(), stderr.decode()


async def test_full_lifecycle_init_validate_run_status_findings_report(
    tmp_path: Path,
) -> None:
    code, out, _ = await _run_cli("init", "--output", "engagement.yaml", cwd=tmp_path)
    assert code == 0
    assert "Created engagement config" in out

    code, out, _ = await _run_cli("validate", "engagement.yaml", cwd=tmp_path)
    assert code == 0
    assert "Configuration valid." in out

    code, out, _ = await _run_cli("run", "engagement.yaml", "--non-interactive", cwd=tmp_path)
    assert code == 0, out
    run_id = next(line.split("RunId: ")[1].strip() for line in out.splitlines() if "RunId:" in line)

    code, out, _ = await _run_cli("status", run_id, cwd=tmp_path)
    assert code == 0
    assert "FINISHED" in out

    code, out, _ = await _run_cli("findings", "list", run_id, cwd=tmp_path)
    assert code == 0

    code, out, _ = await _run_cli("report", "generate", run_id, cwd=tmp_path)
    assert code == 0
    assert "Report generated" in out
    assert (tmp_path / "reports" / run_id / "report.md").exists()


async def test_run_with_json_flag_streams_valid_json_lines(tmp_path: Path) -> None:
    (tmp_path / "engagement.yaml").write_text(_OPEN_ENGAGEMENT, encoding="utf-8")
    code, out, _ = await _run_cli(
        "run", "engagement.yaml", "--non-interactive", "--json", cwd=tmp_path
    )
    assert code == 0
    lines = [line for line in out.splitlines() if line.strip()]
    assert lines
    for line in lines:
        json.loads(line)  # every line is a standalone valid JSON object


async def test_roe_denied_engagement_denies_and_still_exits_zero(tmp_path: Path) -> None:
    """A policy DENY is a correct outcome, not a CLI failure (section 20)."""
    (tmp_path / "engagement.yaml").write_text(_DENY_ENGAGEMENT, encoding="utf-8")
    code, out, _ = await _run_cli("run", "engagement.yaml", "--non-interactive", cwd=tmp_path)
    assert code == 0
    assert "Actions executed: 0" in out


async def test_require_approval_then_approve_resumes_the_same_run(tmp_path: Path) -> None:
    (tmp_path / "engagement.yaml").write_text(_APPROVAL_ENGAGEMENT, encoding="utf-8")

    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "redteam.app.cli",
        "run",
        "engagement.yaml",
        cwd=str(tmp_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        approval_id = await _wait_for_pending_approval(tmp_path)

        code, out, err = await _run_cli("approvals", "approve", approval_id, cwd=tmp_path)
        assert code == 0, err
        assert "-> approved" in out

        stdout, _stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
        assert proc.returncode == 0
        text = stdout.decode()
        assert "ActionApproved" in text
        assert "Actions executed: 1" in text
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()


async def test_require_approval_then_deny_never_executes(tmp_path: Path) -> None:
    (tmp_path / "engagement.yaml").write_text(_APPROVAL_ENGAGEMENT, encoding="utf-8")

    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "redteam.app.cli",
        "run",
        "engagement.yaml",
        cwd=str(tmp_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        approval_id = await _wait_for_pending_approval(tmp_path)

        code, out, err = await _run_cli("approvals", "deny", approval_id, cwd=tmp_path)
        assert code == 0, err
        assert "-> rejected" in out

        stdout, _stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
        assert proc.returncode == 0
        text = stdout.decode()
        assert "ActionDenied" in text
        assert "Actions executed: 0" in text
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()


async def test_no_subcommand_dispatches_to_interactive_not_run(tmp_path: Path) -> None:
    """Section 23/26 backward compatibility: bare ``redteam`` takes the
    new interactive path (banner, then a config-error exit once stdin is
    immediately closed/empty) -- it must never resolve to `run`'s own
    argparse-required-subcommand error, and `redteam run ...` itself
    (exercised by every other test in this file) never goes near this
    path at all."""

    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "redteam.app.cli",
        cwd=str(tmp_path),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
    out = stdout.decode()

    assert "REDTEAM" in out
    assert "AI-Powered Security Assessment Framework" in out
    assert "no command handler registered" not in out
    assert proc.returncode == 2  # EXIT_CONFIG_ERROR, not argparse's usage error


async def _wait_for_pending_approval(cwd: Path, *, timeout_seconds: float = 20.0) -> str:
    deadline = asyncio.get_event_loop().time() + timeout_seconds
    approvals_dir = cwd / "data" / "approvals"
    while asyncio.get_event_loop().time() < deadline:
        if approvals_dir.exists():
            files = list(approvals_dir.glob("appr_*.json"))
            if files:
                payload = json.loads(files[0].read_text(encoding="utf-8"))
                if payload.get("status") == "pending":
                    return payload["approval_id"]
        await asyncio.sleep(0.2)
    raise TimeoutError("no pending approval appeared in time")
