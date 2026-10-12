"""CLI `run` must use the canonical `build_application()` path, never a
CLI-only shortcut — the whole point of this task. These tests patch
`build_application` itself and assert it (not `build_runtime_deps`) is
what `commands.run` actually calls, with the configured LLM/MCP settings
flowing through untouched.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from redteam.app.cli.commands import run as run_command


@pytest.fixture(autouse=True)
def explicit_legacy_runtime(monkeypatch):
    """These fixtures exercise the retained legacy CLI/schema, explicitly selected."""
    monkeypatch.setenv("REDTEAM_RUNTIME_BACKEND", "legacy")


def _engagement_yaml(tmp_path: Path) -> Path:
    path = tmp_path / "engagement.yaml"
    path.write_text(
        """
name: "Wiring Test"
scope:
  allowed_hosts: ["a.local"]
execution_budget:
  max_actions: 1
rate_limit_policy:
  max_actions_per_minute: 10
approval_policy:
  require_approval_for: []
""",
        encoding="utf-8",
    )
    return path








async def test_run_command_fails_fast_on_bad_mcp_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A configured-but-unreachable MCP server id must abort the run
    (exit 2) -- never silently fall back to the ExampleMCPAdapter."""
    from redteam.capability.settings import MCPConfigurationError

    engagement_path = _engagement_yaml(tmp_path)
    monkeypatch.setenv("REDTEAM_LLM_PROVIDER", "fake")
    monkeypatch.setenv("REDTEAM_MCP_SERVERS", "broken-server")

    monkeypatch.setattr(
        run_command,
        "build_application",
        AsyncMock(
            side_effect=MCPConfigurationError(
                "required environment variable REDTEAM_MCP_BROKEN_SERVER_KIND is not set"
            )
        ),
    )

    args = argparse.Namespace(
        engagement_path=str(engagement_path),
        non_interactive=True,
        max_steps=None,
        log_level=None,
        json=False,
    )
    exit_code = await run_command.run(args)

    assert exit_code == 2  # EXIT_CONFIG_ERROR
