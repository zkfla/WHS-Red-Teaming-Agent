"""`redteam tools list|health` — reads the live Registry, no separate model."""

from __future__ import annotations

import argparse
import json

import pytest

from redteam.app.cli._shared import EXIT_OK
from redteam.app.cli.commands import tools as tools_command


@pytest.fixture(autouse=True)
def _no_mcp_servers_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    """These tests exercise the "nothing configured" reality -- an honest
    empty catalog, never a silent ExampleMCPAdapter substitute -- so a
    dev/CI machine's own ``REDTEAM_MCP_SERVERS`` must not leak in."""
    monkeypatch.delenv("REDTEAM_MCP_SERVERS", raising=False)


def _args(*, json_mode=False):  # noqa: ANN001, ANN202
    return argparse.Namespace(json=json_mode)


async def test_empty_registry_shows_no_tools_and_warns(capsys) -> None:
    """No MCP server configured -> an honest empty catalog, never a
    silent ExampleMCPAdapter substitute (production must never hide a
    missing/misconfigured MCP connection behind a fake working tool)."""
    exit_code = await tools_command.run_list(_args())
    assert exit_code == EXIT_OK
    out, err = capsys.readouterr()
    assert "recon.ping" not in out
    assert "(none)" in out
    assert "WARNING" in err
    assert "ExampleMCPAdapter" not in err


async def test_list_is_deterministic_across_calls(capsys) -> None:
    await tools_command.run_list(_args(json_mode=True))
    first = json.loads(capsys.readouterr().out)
    await tools_command.run_list(_args(json_mode=True))
    second = json.loads(capsys.readouterr().out)
    assert first == second


async def test_health_with_no_servers_shows_none_registered(capsys) -> None:
    exit_code = await tools_command.run_health(_args())
    assert exit_code == EXIT_OK
    out = capsys.readouterr().out
    assert "example-mcp" not in out
    assert "(no MCP servers registered)" in out


async def test_tools_json_output_is_valid(capsys) -> None:
    exit_code = await tools_command.run_list(_args(json_mode=True))
    assert exit_code == EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload == []
