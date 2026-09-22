"""`redteam tools list|health` — reads the live Registry, no separate model."""

from __future__ import annotations

import argparse
import json

from redteam.app.cli._shared import EXIT_OK
from redteam.app.cli.commands import tools as tools_command


def _args(*, config_path=None, engagement_path=None, json_mode=False):  # noqa: ANN001, ANN202
    return argparse.Namespace(
        config_path=config_path, engagement_path=engagement_path, json=json_mode
    )


async def test_empty_registry_uses_example_adapter_and_warns(capsys) -> None:
    exit_code = await tools_command.run_list(_args())
    assert exit_code == EXIT_OK
    out, err = capsys.readouterr()
    assert "recon.ping" in out
    assert "WARNING" in err
    assert "ExampleMCPAdapter" in err


async def test_list_is_deterministic_across_calls(capsys) -> None:
    await tools_command.run_list(_args(json_mode=True))
    first = json.loads(capsys.readouterr().out)
    await tools_command.run_list(_args(json_mode=True))
    second = json.loads(capsys.readouterr().out)
    assert first == second


async def test_health_reports_example_server_healthy(capsys) -> None:
    exit_code = await tools_command.run_health(_args())
    assert exit_code == EXIT_OK
    out = capsys.readouterr().out
    assert "example-mcp" in out
    assert "HEALTHY" in out


async def test_tools_json_output_is_valid(capsys) -> None:
    exit_code = await tools_command.run_list(_args(json_mode=True))
    assert exit_code == EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert isinstance(payload, list)
    assert payload[0]["tool_id"] == "recon.ping"
