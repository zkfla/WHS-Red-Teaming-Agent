"""CLI `run` must use the canonical `build_application()` path, never a
CLI-only shortcut — the whole point of this task. These tests patch
`build_application` itself and assert it (not `build_runtime_deps`) is
what `commands.run` actually calls, with the configured LLM/MCP settings
flowing through untouched.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from redteam.app.cli.commands import run as run_command
from redteam.core.contracts.engagement import (
    ApprovalPolicy,
    Engagement,
    EngagementScope,
    ExecutionBudget,
    RateLimitPolicy,
)
from redteam.core.enums import AgentPhase
from redteam.core.ids import EngagementId, RunId
from redteam.orchestration.state import RuntimeState
from tests.fakes import build_test_application


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


def _finished_runtime() -> RuntimeState:
    return RuntimeState(
        run_id=RunId("run_wiring"),
        engagement_id=EngagementId("eng_wiring"),
        phase=AgentPhase.FINISHED,
        termination_reason="no_actionable_plan",
        actions_executed=0,
    )


async def test_run_command_calls_build_application_not_build_runtime_deps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # build_application itself is mocked below, but the real
    # load_app_config_for_run() still runs first and now requires an
    # explicit provider (section 4/20) -- there is no --config app.yaml
    # path any more.
    monkeypatch.setenv("REDTEAM_LLM_PROVIDER", "fake")
    engagement_path = _engagement_yaml(tmp_path)
    engagement = Engagement(
        engagement_id=EngagementId("eng_wiring"),
        name="Wiring Test",
        scope=EngagementScope(allowed_hosts=["a.local"]),
        execution_budget=ExecutionBudget(max_actions=1),
        rate_limit_policy=RateLimitPolicy(max_actions_per_minute=10),
        approval_policy=ApprovalPolicy(),
        created_at=datetime.now(timezone.utc),
    )
    deps = build_test_application()

    build_application_mock = AsyncMock(return_value=(engagement, deps))
    monkeypatch.setattr(run_command, "build_application", build_application_mock)
    monkeypatch.setattr(
        run_command,
        "run_engagement",
        AsyncMock(return_value=_finished_runtime()),
    )
    # commands.run doesn't even import build_runtime_deps -- the CLI-only
    # dependency graph this task's section 1 explicitly disallows.
    assert not hasattr(run_command, "build_runtime_deps")

    args = argparse.Namespace(
        engagement_path=str(engagement_path),
        non_interactive=True,
        max_steps=None,
        log_level=None,
        json=False,
    )
    exit_code = await run_command.run(args)

    assert exit_code == 0
    build_application_mock.assert_awaited_once()
    call = build_application_mock.await_args
    assert call is not None
    called_config = call.args[0]
    assert called_config.engagement.name == "Wiring Test"


async def test_run_command_propagates_configured_llm_and_mcp_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Real ``REDTEAM_LLM_*``/``REDTEAM_MCP_SERVERS`` environment values must
    reach `build_application` unchanged -- not silently dropped/defaulted."""
    engagement_path = _engagement_yaml(tmp_path)
    monkeypatch.setenv("REDTEAM_LLM_PROVIDER", "litellm")
    monkeypatch.setenv("REDTEAM_LLM_MODEL", "gpt-4o-mini")
    monkeypatch.setenv("REDTEAM_MCP_SERVERS", "web-example")

    engagement = Engagement(
        engagement_id=EngagementId("eng_wiring"),
        name="Wiring Test",
        scope=EngagementScope(allowed_hosts=["a.local"]),
        execution_budget=ExecutionBudget(max_actions=1),
        rate_limit_policy=RateLimitPolicy(max_actions_per_minute=10),
        approval_policy=ApprovalPolicy(),
        created_at=datetime.now(timezone.utc),
    )
    deps = build_test_application()
    build_application_mock = AsyncMock(return_value=(engagement, deps))
    monkeypatch.setattr(run_command, "build_application", build_application_mock)
    monkeypatch.setattr(
        run_command, "run_engagement", AsyncMock(return_value=_finished_runtime())
    )

    args = argparse.Namespace(
        engagement_path=str(engagement_path),
        non_interactive=True,
        max_steps=None,
        log_level=None,
        json=False,
    )
    await run_command.run(args)

    call = build_application_mock.await_args
    assert call is not None
    called_config = call.args[0]
    assert called_config.llm.provider == "litellm"
    assert called_config.llm.default_model == "gpt-4o-mini"
    assert called_config.capability.mcp_servers == ["web-example"]


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
