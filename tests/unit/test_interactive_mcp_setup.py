"""``commands.interactive._ensure_mcp_config`` / ``_write_recon_policy``.

Regression guard for a real bug: the auto-setup wizard wrote
``REDTEAM_MCP_RECON_PASSIVE_*`` without ``TOOL_OPERATIONS_JSON``, so every
discovered tool fell back to its own literal name as its "operation"
(``capability.bootstrap``) instead of the canonical
``service_discovery``/``endpoint_discovery`` the Recon Agent's candidates
actually ask for -- every candidate was then rejected as
``operation_unsupported`` and the Mission blocked with zero actions
executed, even though the MCP server itself connected and discovered tools
fine. None of the existing interactive-flow tests caught this because they
all script a decline ("n") to this prompt.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import cast

import pytest
from rich.console import Console

from redteam.app.cli.commands import interactive
from redteam.capability.servers.config import ServerPolicy
from redteam.capability.servers.recon import create_server as create_recon_server
from redteam.capability.settings import MCPServerSettings
from redteam.core.config import CapabilityConfig, EngagementConfig

_MINIMAL_RECON_POLICY = ServerPolicy.model_validate(
    {
        "targets": {"t": {"origin": "http://127.0.0.1", "allowed_paths": ["/"]}},
        "allowed_networks": ["127.0.0.1/32"],
    }
)


class _FakeConsole:
    def __init__(self, answers: list[str]) -> None:
        self._answers = list(answers)
        self.transcript: list[str] = []

    def print(self, *args: object, **_kwargs: object) -> None:
        for arg in args:
            self.transcript.append(str(arg))

    def input(self, prompt: str = "") -> str:
        self.transcript.append(str(prompt))
        if not self._answers:
            raise EOFError("no more scripted answers")
        return self._answers.pop(0)


def _engagement(host: str) -> EngagementConfig:
    return EngagementConfig.model_validate(
        {
            "name": "setup-test",
            "scope": {"allowed_hosts": [host]},
            "execution_budget": {"max_actions": 5},
        }
    )


# Every key `_ensure_mcp_config`'s accept path can set (mirrors the
# `settings` dict in `commands.interactive`, plus `REDTEAM_MCP_SERVERS`
# itself). `monkeypatch.delenv(key, raising=False)` on a key that is not
# yet in `os.environ` records NO undo at all (verified directly -- it is a
# true no-op, not a "restore to absent" guard), so a key this test is the
# *first* to ever set would otherwise leak into the real process
# environment for the rest of the pytest session, corrupting any later
# test that copies `os.environ` verbatim into a subprocess (e.g.
# `tests/integration/test_cli_smoke.py`). An explicit `os.environ.pop`
# teardown is the only thing that actually guarantees cleanup here.
_RECON_PASSIVE_ENV_KEYS = (
    "REDTEAM_MCP_SERVERS",
    "REDTEAM_MCP_RECON_PASSIVE_KIND",
    "REDTEAM_MCP_RECON_PASSIVE_TRANSPORT",
    "REDTEAM_MCP_RECON_PASSIVE_COMMAND",
    "REDTEAM_MCP_RECON_PASSIVE_ARGS_JSON",
    "REDTEAM_MCP_RECON_PASSIVE_RISK_LEVEL",
    "REDTEAM_MCP_RECON_PASSIVE_TOOL_OPERATIONS_JSON",
)


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):  # noqa: ANN201
    monkeypatch.chdir(tmp_path)
    for key in _RECON_PASSIVE_ENV_KEYS:
        os.environ.pop(key, None)
    yield
    for key in _RECON_PASSIVE_ENV_KEYS:
        os.environ.pop(key, None)


async def test_recon_passive_tool_operations_cover_every_discovered_tool() -> None:
    """The exact regression this file exists for: `build_mcp_tool_gateway`
    requires `TOOL_OPERATIONS_JSON`'s keys to match the server's discovered
    tool names exactly -- if `servers/recon.py` ever gains/loses a tool
    without this binding being updated, this must fail, not the next real
    run with a silent `operation_unsupported` rejection.
    """
    server = create_recon_server(_MINIMAL_RECON_POLICY)
    discovered_names = {tool.name for tool in await server.list_tools()}
    assert discovered_names == set(interactive._RECON_PASSIVE_TOOL_OPERATIONS)


async def test_accepting_writes_a_complete_working_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    console = _FakeConsole(["y"])
    capability_config = await interactive._ensure_mcp_config(
        cast(Console, console), _engagement("127.0.0.1:5000"), CapabilityConfig()
    )

    assert capability_config.mcp_servers == ["recon-passive"]
    assert os.environ["REDTEAM_MCP_SERVERS"] == "recon-passive"

    # The actual bug: this key must be present and bind every discovered
    # tool name, or `MCPServerSettings.from_environment` falls back to no
    # canonical operation at all.
    bindings = json.loads(os.environ["REDTEAM_MCP_RECON_PASSIVE_TOOL_OPERATIONS_JSON"])
    assert bindings["http_probe"] == ["service_discovery"]
    assert bindings["html_inventory"] == ["endpoint_discovery"]

    # A real `MCPServerSettings.from_environment` call must succeed end to
    # end with exactly what was written -- not just "some JSON was written".
    settings = MCPServerSettings.from_environment("recon-passive")
    assert settings.tool_operations is not None
    assert settings.tool_operations["http_probe"] == ("service_discovery",)

    env_text = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "REDTEAM_MCP_RECON_PASSIVE_TOOL_OPERATIONS_JSON=" in env_text


async def test_declining_leaves_capability_config_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    console = _FakeConsole(["n"])
    original = CapabilityConfig()

    result = await interactive._ensure_mcp_config(
        cast(Console, console), _engagement("127.0.0.1:5000"), original
    )

    assert result is original
    assert "REDTEAM_MCP_SERVERS" not in os.environ
    assert not (tmp_path / ".env").exists()


async def test_already_configured_never_prompts(monkeypatch: pytest.MonkeyPatch) -> None:
    # `_ensure_runtime_config` (called before this, in the real `_run()`
    # flow) already derived `capability_config` from the environment when
    # `REDTEAM_MCP_SERVERS` is set -- this function must trust that and
    # pass it through unchanged, never re-prompt.
    monkeypatch.setenv("REDTEAM_MCP_SERVERS", "web-example")
    already_configured = CapabilityConfig(mcp_servers=["web-example"])
    console = _FakeConsole([])  # any console.input() call would raise EOFError here

    result = await interactive._ensure_mcp_config(
        cast(Console, console), _engagement("127.0.0.1:5000"), already_configured
    )

    assert result is already_configured


async def test_no_scope_declared_is_a_no_op(monkeypatch: pytest.MonkeyPatch) -> None:
    console = _FakeConsole([])
    original = CapabilityConfig()
    no_scope = EngagementConfig.model_validate(
        {"name": "no-scope", "execution_budget": {"max_actions": 1}}
    )

    result = await interactive._ensure_mcp_config(
        cast(Console, console), no_scope, original
    )

    assert result is original
