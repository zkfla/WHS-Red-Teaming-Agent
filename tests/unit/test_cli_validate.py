"""`redteam validate` — schema/ROE/capability checks, no target touched."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from redteam.app.cli._shared import EXIT_CONFIG_ERROR, EXIT_OK
from redteam.app.cli.commands import validate as validate_command


def _write(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def _args(engagement_path: Path, *, config_path: str | None = None, json_mode: bool = False):
    return argparse.Namespace(
        engagement_path=str(engagement_path), config_path=config_path, json=json_mode
    )


async def test_valid_engagement_passes(tmp_path: Path, capsys) -> None:
    path = _write(
        tmp_path,
        "engagement.yaml",
        """
name: "ok"
scope:
  allowed_hosts: ["a.local"]
execution_budget:
  max_actions: 1
rate_limit_policy:
  max_actions_per_minute: 10
approval_policy:
  require_approval_for: []
""",
    )
    exit_code = await validate_command.run(_args(path))
    assert exit_code == EXIT_OK
    assert "Configuration valid." in capsys.readouterr().out


async def test_invalid_schema_fails(tmp_path: Path) -> None:
    path = _write(tmp_path, "engagement.yaml", "name: 123\nexecution_budget: not-a-dict\n")
    exit_code = await validate_command.run(_args(path))
    assert exit_code == EXIT_CONFIG_ERROR


async def test_invalid_roe_operation_is_reported(tmp_path: Path, capsys) -> None:
    path = _write(
        tmp_path,
        "engagement.yaml",
        """
name: "bad-roe"
scope:
  allowed_hosts: ["a.local"]
rules_of_engagement:
  - name: "typo"
    denied_operations: ["recon.portscan"]
execution_budget:
  max_actions: 1
rate_limit_policy:
  max_actions_per_minute: 10
approval_policy:
  require_approval_for: []
""",
    )
    exit_code = await validate_command.run(_args(path))
    assert exit_code == EXIT_CONFIG_ERROR
    err = capsys.readouterr().err
    assert "recon.portscan" in err
    assert "not in the canonical v1 contract" in err


async def test_invalid_capability_config_is_reported(tmp_path: Path, monkeypatch) -> None:
    engagement_path = _write(
        tmp_path,
        "engagement.yaml",
        """
name: "bad-capability"
scope:
  allowed_hosts: ["a.local"]
execution_budget:
  max_actions: 1
rate_limit_policy:
  max_actions_per_minute: 10
approval_policy:
  require_approval_for: []
""",
    )
    app_config_path = _write(
        tmp_path, "app.yaml", 'capability:\n  mcp_servers: ["missing-server"]\n'
    )
    monkeypatch.delenv("REDTEAM_MCP_MISSING_SERVER_KIND", raising=False)

    exit_code = await validate_command.run(_args(engagement_path, config_path=str(app_config_path)))
    assert exit_code == EXIT_CONFIG_ERROR


async def test_prohibited_techniques_is_flagged_as_unenforceable(tmp_path: Path, capsys) -> None:
    path = _write(
        tmp_path,
        "engagement.yaml",
        """
name: "unenforceable"
scope:
  allowed_hosts: ["a.local"]
rules_of_engagement:
  - name: "no-brute-force"
    prohibited_techniques: ["brute_force"]
execution_budget:
  max_actions: 1
rate_limit_policy:
  max_actions_per_minute: 10
approval_policy:
  require_approval_for: []
""",
    )
    exit_code = await validate_command.run(_args(path))
    assert exit_code == EXIT_CONFIG_ERROR
    assert "prohibited_techniques" in capsys.readouterr().err


async def test_no_allowlist_is_flagged(tmp_path: Path, capsys) -> None:
    path = _write(
        tmp_path,
        "engagement.yaml",
        """
name: "no-scope"
execution_budget:
  max_actions: 1
rate_limit_policy:
  max_actions_per_minute: 10
approval_policy:
  require_approval_for: []
""",
    )
    exit_code = await validate_command.run(_args(path))
    assert exit_code == EXIT_CONFIG_ERROR
    assert "default-deny" in capsys.readouterr().err


async def test_json_output_is_valid_json(tmp_path: Path, capsys) -> None:
    path = _write(
        tmp_path,
        "engagement.yaml",
        """
name: "ok"
scope:
  allowed_hosts: ["a.local"]
execution_budget:
  max_actions: 1
rate_limit_policy:
  max_actions_per_minute: 10
approval_policy:
  require_approval_for: []
""",
    )
    exit_code = await validate_command.run(_args(path, json_mode=True))
    assert exit_code == EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["valid"] is True
