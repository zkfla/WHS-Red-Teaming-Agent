"""Interactive slash-command registry.

Local commands the CLI/Application layer handles directly, without ever
reaching the Agent Runtime (sections 3, 13-15 of the spec this module
implements). Every read-only command below calls the *exact same*
Application Service function the standalone ``redteam <name> ...``
subcommand does (``commands.tools.run_list``, ``commands.status.run``,
...) -- never a re-implementation, so ``/tools`` and ``redteam tools
list`` can never drift into two different answers.

``InteractiveSession`` is a thin View State (section 17): each command
still re-derives its own answer from the same durable files/state
``redteam <name> ...`` reads, so a stale in-memory session can only ever
supply a *default* argument (the last run id, the current config paths)
-- it is never itself trusted as the answer.
"""

from __future__ import annotations

import argparse
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

from rich.console import Console

from redteam.app.cli import theme
from redteam.app.cli._shared import DEFAULT_ENGAGEMENT_PATH
from redteam.app.cli.commands import approvals as approvals_command
from redteam.app.cli.commands import findings as findings_command
from redteam.app.cli.commands import report as report_command
from redteam.app.cli.commands import roe as roe_command
from redteam.app.cli.commands import status as status_command
from redteam.app.cli.commands import tools as tools_command
from redteam.core.config import EngagementConfig, LLMConfig
from redteam.core.ids import RunId
from redteam.orchestration.state import RuntimeState


@dataclass
class InteractiveSession:
    """Runtime/Event/Service projection only -- never a second source of
    truth (section 17). Populated once at session start and refreshed via
    :meth:`record_run` after each Agent Run this CLI process executes.
    """

    engagement_config: EngagementConfig
    llm_config: LLMConfig
    engagement_path: Path = DEFAULT_ENGAGEMENT_PATH
    last_run_id: RunId | None = None
    last_phase: str | None = None
    last_termination_reason: str | None = None
    last_actions_executed: int = 0
    last_pending_approval_id: str | None = None
    last_observations: list[str] = field(default_factory=list)

    def record_run(self, final: RuntimeState | None) -> None:
        if final is None:
            return
        self.last_run_id = final.run_id
        self.last_phase = final.phase.value
        self.last_termination_reason = final.termination_reason
        self.last_actions_executed = final.actions_executed
        self.last_observations = [item.summary for item in final.recent_observations[-5:]]
        self.last_pending_approval_id = (
            str(final.pending_approval_id) if final.pending_approval_id is not None else None
        )

    def explain_last_run(self) -> str:
        if self.last_run_id is None:
            return "[Agent] No run has been executed in this session yet."
        reason = self.last_termination_reason or "completed"
        summary = (
            f"[Agent] Run {self.last_run_id} is {self.last_phase or 'unknown'} because "
            f"'{reason}'. It executed {self.last_actions_executed} action(s)."
        )
        if self.last_observations:
            summary += "\n  Recent observations:\n" + "\n".join(
                f"  - {item}" for item in self.last_observations
            )
        return summary


class SessionExit(Exception):
    """Raised by ``/exit`` to unwind the interactive loop. Always caught
    inside :func:`dispatch` -- never propagates past this module."""


CommandHandler = Callable[[Console, InteractiveSession, str], Awaitable[None]]


@dataclass(frozen=True)
class InteractiveCommand:
    name: str
    aliases: tuple[str, ...]
    description: str
    handler: CommandHandler


def _run_id_or_last(session: InteractiveSession, rest: str) -> str | None:
    return rest.strip() or (str(session.last_run_id) if session.last_run_id else None)


async def _cmd_help(console: Console, _session: InteractiveSession, _rest: str) -> None:
    console.print("\n[bold]Commands[/bold]\n")
    for command in COMMANDS:
        aliases = f" ({', '.join('/' + a for a in command.aliases)})" if command.aliases else ""
        console.print(f"  /{command.name:<10}{aliases}  {command.description}")
    console.print(
        '\n[dim]Other input is interpreted as a question, clarification, '
        "or investigation request for the current engagement.[/dim]"
    )


async def _cmd_status(console: Console, session: InteractiveSession, rest: str) -> None:
    run_id = _run_id_or_last(session, rest)
    if run_id is None:
        console.print("[dim]No run yet this session. Usage: /status <run_id>[/dim]")
        return
    await status_command.run(
        argparse.Namespace(run_id=run_id, approval_dir="data/approvals", json=False)
    )


async def _cmd_tools(console: Console, _session: InteractiveSession, _rest: str) -> None:
    await tools_command.run_list(argparse.Namespace(json=False))


async def _cmd_health(console: Console, _session: InteractiveSession, _rest: str) -> None:
    await tools_command.run_health(argparse.Namespace(json=False))


async def _cmd_approvals(console: Console, session: InteractiveSession, rest: str) -> None:
    run_id = rest.strip() or (str(session.last_run_id) if session.last_run_id else None)
    await approvals_command.run_list(
        argparse.Namespace(
            run_id=run_id, approval_dir="data/approvals", audit_dir="data/audit", json=False
        )
    )


async def _cmd_findings(console: Console, session: InteractiveSession, rest: str) -> None:
    run_id = _run_id_or_last(session, rest)
    if run_id is None:
        console.print("[dim]No run yet this session. Usage: /findings <run_id>[/dim]")
        return
    await findings_command.run_list(argparse.Namespace(run_id=run_id, json=False))


async def _cmd_report(console: Console, session: InteractiveSession, rest: str) -> None:
    run_id = _run_id_or_last(session, rest)
    if run_id is None:
        console.print("[dim]No run yet this session. Usage: /report <run_id>[/dim]")
        return
    await report_command.run_generate(
        argparse.Namespace(run_id=run_id, format="md", output_dir="reports")
    )


async def _cmd_roe(console: Console, session: InteractiveSession, _rest: str) -> None:
    await roe_command.run(
        argparse.Namespace(engagement_path=str(session.engagement_path), json=False)
    )


async def _cmd_model(console: Console, session: InteractiveSession, _rest: str) -> None:
    console.print(
        f"Provider: {session.llm_config.provider}\n"
        f"Model:    {session.llm_config.default_model}\n"
        f"[dim]Change it by setting REDTEAM_LLM_PROVIDER/REDTEAM_LLM_MODEL in .env "
        f"and restarting.[/dim]"
    )


async def _cmd_config(console: Console, session: InteractiveSession, _rest: str) -> None:
    console.print(
        f"Engagement:      {session.engagement_config.name}\n"
        f"Engagement file: {session.engagement_path}\n"
        f"LLM provider:    {session.llm_config.provider} (REDTEAM_LLM_PROVIDER/.env)"
    )


async def _cmd_clear(console: Console, _session: InteractiveSession, _rest: str) -> None:
    console.clear()


async def _cmd_exit(_console: Console, _session: InteractiveSession, _rest: str) -> None:
    raise SessionExit()


COMMANDS: tuple[InteractiveCommand, ...] = (
    InteractiveCommand("help", ("?", "h"), "Show this list", _cmd_help),
    InteractiveCommand("status", (), "Show a run's status (default: last run)", _cmd_status),
    InteractiveCommand("tools", (), "List registered Capability tools", _cmd_tools),
    InteractiveCommand("health", (), "Show MCP server health", _cmd_health),
    InteractiveCommand("approvals", (), "List pending approvals", _cmd_approvals),
    InteractiveCommand("findings", (), "List findings (default: last run)", _cmd_findings),
    InteractiveCommand("report", (), "Generate the report (default: last run)", _cmd_report),
    InteractiveCommand("roe", (), "Show Rules of Engagement", _cmd_roe),
    InteractiveCommand("model", (), "Show the configured LLM model", _cmd_model),
    InteractiveCommand("config", (), "Show engagement/app config paths", _cmd_config),
    InteractiveCommand("clear", ("cls",), "Clear the screen", _cmd_clear),
    InteractiveCommand("exit", ("quit", "q"), "End the interactive session", _cmd_exit),
)


def _build_registry() -> dict[str, InteractiveCommand]:
    registry: dict[str, InteractiveCommand] = {}
    for command in COMMANDS:
        registry[command.name] = command
        for alias in command.aliases:
            registry[alias] = command
    return registry


_REGISTRY = _build_registry()


async def dispatch(console: Console, session: InteractiveSession, text: str) -> bool:
    """Handle one ``/command`` line.

    Returns ``False`` only for ``/exit`` (the session should end); every
    other outcome -- success, unknown command, or a handler raising --
    returns ``True`` (section 21: a recoverable error shows a message and
    returns to the prompt, it never ends the session).
    """
    body = text[1:].strip()
    name, _, rest = body.partition(" ")
    name = name.lower()
    if not name:
        console.print("[dim]Type /help for a list of commands.[/dim]")
        return True
    command = _REGISTRY.get(name)
    if command is None:
        console.print(f"[{theme.DANGER}]Unknown command: /{name}[/{theme.DANGER}] (try /help)")
        return True
    try:
        await command.handler(console, session, rest.strip())
    except SessionExit:
        return False
    except Exception as exc:  # noqa: BLE001 - one bad command must never end the session
        style = theme.DANGER
        console.print(f"[{style}]{theme.ICON_FAILURE} /{name} failed: {exc}[/{style}]")
    return True
