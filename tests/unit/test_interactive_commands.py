"""Unit tests for the interactive slash-command registry
(``redteam.app.cli.interactive_commands``).

Covers section 29's "Unit" list for this feature: Command Registry,
Input routing, Session lifecycle, Error recovery, Exit. Renderer/event
rendering itself is already covered by ``tests/unit/test_cli_renderer.py``
(unchanged by this feature).
"""

from __future__ import annotations

import argparse

import pytest

from redteam.app.cli import interactive_commands
from redteam.app.cli.interactive_commands import (
    COMMANDS,
    InteractiveCommand,
    InteractiveSession,
    SessionExit,
    dispatch,
)
from redteam.core.config import EngagementConfig, LLMConfig
from redteam.core.enums import AgentPhase
from redteam.core.ids import EngagementId, RunId
from redteam.orchestration.state import RuntimeState

# No `pytestmark = pytest.mark.asyncio` here: this file mixes sync and
# async tests, and `asyncio_mode = "auto"` (pyproject.toml) already
# detects `async def` tests without it.


class _FakeConsole:
    """Minimal duck-typed stand-in, same shape as
    ``tests/integration/test_cli_interactive.py``'s own fake."""

    def __init__(self) -> None:
        self.lines: list[str] = []
        self.cleared = False

    def print(self, *args: object, **_kwargs: object) -> None:
        for arg in args:
            self.lines.append(str(arg))

    def clear(self) -> None:
        self.cleared = True


def _session(**overrides: object) -> InteractiveSession:
    defaults: dict[str, object] = {
        "engagement_config": EngagementConfig.model_validate(
            {
                "name": "VulnBank",
                "scope": {"allowed_hosts": ["example.local"]},
                "execution_budget": {"max_actions": 5},
            }
        ),
        "llm_config": LLMConfig(provider="fake", default_model="gpt-4o-mini"),
    }
    defaults.update(overrides)
    return InteractiveSession(**defaults)  # type: ignore[arg-type]


# --- Command Registry --------------------------------------------------


def test_registry_covers_every_documented_command() -> None:
    names = {command.name for command in COMMANDS}
    assert names == {
        "help", "status", "tools", "health", "approvals", "findings",
        "report", "roe", "model", "config", "clear", "exit",
    }


def test_registry_names_are_unique() -> None:
    names = [command.name for command in COMMANDS]
    assert len(names) == len(set(names))


def test_registry_is_not_a_giant_if_elif_chain() -> None:
    """Structural guard (section 14): dispatch must resolve through a
    dict lookup, not a chain of string comparisons against the command
    name -- adding a new command must never require editing `dispatch`.
    """
    registry = interactive_commands._build_registry()
    assert isinstance(registry, dict)
    for command in COMMANDS:
        assert registry[command.name] is command
        for alias in command.aliases:
            assert registry[alias] is command


@pytest.mark.parametrize(
    ("alias", "canonical"),
    [("?", "help"), ("h", "help"), ("cls", "clear"), ("quit", "exit"), ("q", "exit")],
)
async def test_aliases_resolve_to_the_same_command(alias: str, canonical: str) -> None:
    console = _FakeConsole()
    session = _session()
    calls: list[str] = []

    async def _record(_console, _session, _rest) -> None:  # noqa: ANN001
        calls.append("called")

    fake_registry = {canonical: InteractiveCommand(canonical, (alias,), "test", _record)}
    fake_registry[alias] = fake_registry[canonical]
    original = interactive_commands._REGISTRY
    interactive_commands._REGISTRY = fake_registry
    try:
        assert await dispatch(console, session, f"/{alias}") is True
    finally:
        interactive_commands._REGISTRY = original
    assert calls == ["called"]


# --- Input routing / dispatch -------------------------------------------


async def test_dispatch_unknown_command_recovers_without_ending_session() -> None:
    console = _FakeConsole()
    session = _session()
    result = await dispatch(console, session, "/bogus")
    assert result is True
    assert any("Unknown command" in line for line in console.lines)


async def test_dispatch_bare_slash_shows_a_hint() -> None:
    console = _FakeConsole()
    session = _session()
    result = await dispatch(console, session, "/")
    assert result is True
    assert any("/help" in line for line in console.lines)


async def test_dispatch_is_case_insensitive() -> None:
    console = _FakeConsole()
    session = _session()
    assert await dispatch(console, session, "/HELP") is True
    assert any("Commands" in line for line in console.lines)


# --- Error recovery -------------------------------------------------------


async def test_dispatch_handler_exception_is_recovered_not_propagated() -> None:
    console = _FakeConsole()
    session = _session()

    async def _boom(_console, _session, _rest) -> None:  # noqa: ANN001
        raise RuntimeError("simulated failure")

    original = interactive_commands._REGISTRY
    interactive_commands._REGISTRY = {
        "boom": InteractiveCommand("boom", (), "raises", _boom)
    }
    try:
        result = await dispatch(console, session, "/boom")
    finally:
        interactive_commands._REGISTRY = original

    assert result is True  # a bad command must never end the session (section 21)
    assert any("failed" in line.lower() for line in console.lines)


# --- Exit -------------------------------------------------------------


async def test_dispatch_exit_ends_the_session() -> None:
    console = _FakeConsole()
    session = _session()
    assert await dispatch(console, session, "/exit") is False


async def test_exit_command_raises_session_exit_directly() -> None:
    exit_command = next(c for c in COMMANDS if c.name == "exit")
    with pytest.raises(SessionExit):
        await exit_command.handler(_FakeConsole(), _session(), "")


# --- Session lifecycle / view-state projection ----------------------------


def test_record_run_projects_runtime_state_fields() -> None:
    session = _session()
    final = RuntimeState(
        run_id=RunId("run_abc123"),
        engagement_id=EngagementId("eng_abc123"),
        phase=AgentPhase.FINISHED,
        termination_reason="no_actionable_plan",
        actions_executed=3,
    )

    session.record_run(final)

    assert session.last_run_id == RunId("run_abc123")
    assert session.last_phase == "finished"
    assert session.last_termination_reason == "no_actionable_plan"
    assert session.last_actions_executed == 3


def test_record_run_with_none_is_a_no_op() -> None:
    session = _session(last_run_id=RunId("run_prior"))
    session.record_run(None)
    assert session.last_run_id == RunId("run_prior")


async def test_status_defaults_to_last_run_id_when_no_argument_given(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    async def _fake_status_run(args: argparse.Namespace) -> int:
        captured["run_id"] = args.run_id
        return 0

    monkeypatch.setattr(interactive_commands.status_command, "run", _fake_status_run)
    session = _session(last_run_id=RunId("run_from_session"))

    result = await dispatch(_FakeConsole(), session, "/status")

    assert result is True
    assert captured["run_id"] == "run_from_session"


async def test_status_with_no_history_and_no_argument_shows_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = False

    async def _fake_status_run(_args: argparse.Namespace) -> int:
        nonlocal called
        called = True
        return 0

    monkeypatch.setattr(interactive_commands.status_command, "run", _fake_status_run)
    console = _FakeConsole()
    session = _session()  # last_run_id is None

    await dispatch(console, session, "/status")

    assert called is False
    assert any("No run yet" in line for line in console.lines)


async def test_status_explicit_run_id_overrides_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    async def _fake_status_run(args: argparse.Namespace) -> int:
        captured["run_id"] = args.run_id
        return 0

    monkeypatch.setattr(interactive_commands.status_command, "run", _fake_status_run)
    session = _session(last_run_id=RunId("run_stale"))

    await dispatch(_FakeConsole(), session, "/status run_explicit")

    assert captured["run_id"] == "run_explicit"


async def test_tools_command_delegates_to_the_shared_tools_run_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``/tools`` calls the exact same ``tools_command.run_list`` the
    standalone ``redteam tools list`` uses -- both read
    ``REDTEAM_MCP_SERVERS`` straight from the environment, no CLI/session
    config-path plumbing (there is no application-config file any more)."""
    captured: dict[str, object] = {}

    async def _fake_run_list(args: argparse.Namespace) -> int:
        captured["json"] = args.json
        return 0

    monkeypatch.setattr(interactive_commands.tools_command, "run_list", _fake_run_list)
    session = _session()

    await dispatch(_FakeConsole(), session, "/tools")

    assert captured["json"] is False


async def test_clear_command_clears_the_console() -> None:
    console = _FakeConsole()
    await dispatch(console, _session(), "/clear")
    assert console.cleared is True
