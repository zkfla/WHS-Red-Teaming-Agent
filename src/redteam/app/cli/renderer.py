"""Rich rendering for the interactive CLI (section 17): Core/Orchestration
only ever publish ``EventEnvelope``s on the existing Event Bus (see
``redteam.core.ports.event_bus``/``redteam.core.contracts.events``) --
nothing here is imported by, or imports from the reverse direction into,
``redteam.orchestration``/``redteam.control``/``redteam.capability`` or
any agent. Swapping this module for a future Web UI renderer would not
touch Core at all.

Default mode hides internal identifiers (``action_id``, ``candidate_key``,
``work_id``, ``invocation_id``, ``priority_components``, ...) -- they are
still in the event payload (nothing here mutates or drops them), just
not printed unless ``verbose=True`` (section 16/22).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from redteam.app.cli import theme
from redteam.app.cli._shared import mask_secrets
from redteam.core.contracts.events import EventEnvelope, EventType
from redteam.orchestration.events import AGENT_NO_EXECUTABLE_CANDIDATES

# Events worth a line in the default (human-readable) stream. Same
# filtering intent as `commands.run._RENDERED_EVENTS`, kept separate
# since that module's own plain-line format is a documented, tested
# backward-compat surface this renderer must not touch (section 23).
_RENDERED_EVENTS = frozenset(
    {
        EventType.RUN_STARTED,
        EventType.ACTION_PROPOSED,
        EventType.ACTION_APPROVED,
        EventType.ACTION_DENIED,
        EventType.APPROVAL_REQUIRED,
        EventType.TOOL_EXECUTION_FINISHED,
        EventType.TOOL_EXECUTION_FAILED,
        EventType.GRAPH_UPDATED,
        EventType.FINDING_CREATED,
        EventType.FINDING_VERIFIED,
        AGENT_NO_EXECUTABLE_CANDIDATES,
    }
)

_OPERATION_PHRASES: dict[str, str] = {
    "service_discovery": "Discovering services on {target}",
    "port_scan": "Scanning ports on {target}",
    "endpoint_discovery": "Discovering endpoints on {target}",
    "http_request": "Testing {target}",
    "auth_attempt": "Testing authentication boundary on {target}",
    "parameter_fuzz": "Fuzzing parameters on {target}",
    "file_upload": "Testing file upload handling on {target}",
    "app_launch": "Launching app on {target}",
    "device_shell": "Running device diagnostic on {target}",
    "runtime_instrumentation": "Instrumenting runtime on {target}",
    "static_analysis": "Analyzing artifact from {target}",
    "response_analysis": "Analyzing responses from {target}",
}


def _agent_label(agent_id: str) -> str:
    words = agent_id.replace("_", "-").split("-")
    return " ".join("API" if word.lower() == "api" else word.capitalize() for word in words)


def _operation_phrase(operation: str, target: Any) -> str:
    fallback = "Testing " + operation.replace("_", " ") + " on {target}"
    template = _OPERATION_PHRASES.get(operation, fallback)
    return template.format(target=target)


class RichEventRenderer:
    """Subscribes to the Event Bus (``EventBus.subscribe("*", ...)``,
    exactly like ``commands.run``'s own subscriber) and prints a
    human-readable line per event. Also tracks the small run counters
    (findings/hypotheses/tool calls) the Preflight/summary panels show
    -- purely from event payloads already published, no new Core state.
    """

    def __init__(self, console: Console, *, verbose: bool = False) -> None:
        self._console = console
        self._verbose = verbose
        self.tool_calls = 0
        self.findings = 0
        self.violations = 0
        self.hypotheses = 0

    async def __call__(self, event: EventEnvelope) -> None:
        self._tally(event)
        if event.event_type not in _RENDERED_EVENTS:
            return
        line = self._render_line(event)
        if line is not None:
            self._console.print(line)
        if self._verbose:
            masked = mask_secrets(event.payload)
            self._console.print(Text(f"    {event.event_type} {masked}", style=theme.MUTED))

    def _tally(self, event: EventEnvelope) -> None:
        if event.event_type in (EventType.TOOL_EXECUTION_FINISHED, EventType.TOOL_EXECUTION_FAILED):
            self.tool_calls += 1
        elif event.event_type in (EventType.FINDING_CREATED, EventType.FINDING_VERIFIED):
            self.findings += 1
            if event.payload.get("severity") in {"high", "critical"}:
                self.violations += 1
        elif event.event_type == EventType.HYPOTHESIS_CREATED:
            self.hypotheses += 1

    def _render_line(self, event: EventEnvelope) -> Text | None:
        timestamp = event.timestamp.strftime("%H:%M:%S")
        payload = event.payload

        if event.event_type == EventType.RUN_STARTED:
            return Text(f"[{timestamp}] ", style=theme.MUTED) + Text(
                "Run started", style=theme.EMPHASIS
            )

        if event.event_type == EventType.ACTION_PROPOSED:
            agent = _agent_label(str(payload.get("agent_id", "agent")))
            phrase = _operation_phrase(
                str(payload.get("operation", "")), payload.get("target", "target")
            )
            return (
                Text(f"[{timestamp}] ", style=theme.MUTED)
                + Text(f"{agent:<14}", style=theme.EMPHASIS)
                + Text(f" {theme.ICON_TRANSITION} ", style=theme.MUTED)
                + Text(phrase)
            )

        if event.event_type == EventType.ACTION_APPROVED:
            return Text(f"[{timestamp}] ", style=theme.MUTED) + Text(
                f"{theme.ICON_SUCCESS} Action approved", style=theme.SUCCESS
            )

        if event.event_type == EventType.ACTION_DENIED:
            reasons = ", ".join(str(r) for r in payload.get("reasons", []) or [])
            suffix = f" ({reasons})" if reasons else ""
            return Text(f"[{timestamp}] ", style=theme.MUTED) + Text(
                f"{theme.ICON_FAILURE} Action blocked by policy{suffix}", style=theme.DANGER
            )

        if event.event_type == EventType.APPROVAL_REQUIRED:
            return Text(f"[{timestamp}] ", style=theme.MUTED) + Text(
                f"{theme.ICON_WARNING} Approval required before this action can run",
                style=theme.WARNING,
            )

        if event.event_type == EventType.TOOL_EXECUTION_FINISHED:
            success = bool(payload.get("success"))
            icon, style = (theme.ICON_SUCCESS, theme.SUCCESS) if success else (
                theme.ICON_FAILURE,
                theme.DANGER,
            )
            text = "Tool call succeeded" if success else "Tool call failed"
            return Text(f"[{timestamp}] ", style=theme.MUTED) + Text(f"{icon} {text}", style=style)

        if event.event_type == EventType.TOOL_EXECUTION_FAILED:
            return Text(f"[{timestamp}] ", style=theme.MUTED) + Text(
                f"{theme.ICON_FAILURE} Tool call failed", style=theme.DANGER
            )

        if event.event_type == EventType.GRAPH_UPDATED:
            node_type = str(payload.get("node_type", "node"))
            node_id = str(payload.get("node_id", ""))
            label = node_id.split(":", 1)[-1] if ":" in node_id else node_id
            return Text(f"[{timestamp}] ", style=theme.MUTED) + Text(
                f"{theme.ICON_TRANSITION} Updating attack graph: {node_type} {label}",
                style=theme.MUTED,
            )

        if event.event_type in (EventType.FINDING_CREATED, EventType.FINDING_VERIFIED):
            title = str(payload.get("title") or payload.get("summary") or "New finding")
            return Text(f"[{timestamp}] ", style=theme.MUTED) + Text(
                f"{theme.ICON_WARNING} Finding: {title}", style=theme.WARNING
            )

        if event.event_type == AGENT_NO_EXECUTABLE_CANDIDATES:
            agent = _agent_label(str(payload.get("agent_id", "agent")))
            drafts = payload.get("drafts_offered", 0)
            return Text(f"[{timestamp}] ", style=theme.MUTED) + Text(
                f"{theme.ICON_WARNING} {agent}: activated, but produced no executable "
                f"candidate ({drafts} draft step(s) had no matching/healthy tool or "
                f"could not be resolved from what's known so far)",
                style=theme.WARNING,
            )

        return None


def render_banner(console: Console) -> None:
    body = Text.from_markup(
        f"[bold]{theme.BANNER_TITLE}[/bold]\n{theme.BANNER_SUBTITLE}", justify="left"
    )
    console.print(Panel(body, expand=False, border_style=theme.EMPHASIS))


def render_config_error(
    console: Console, *, target: str, problem: str, suggested_fix: str | None
) -> None:
    lines = [f"[bold]Target[/bold]\n  {target}", f"\n[bold]Problem[/bold]\n  {problem}"]
    if suggested_fix:
        lines.append(f"\n[bold]Suggested Fix[/bold]\n  {suggested_fix}")
    console.print(
        Panel(
            Text.from_markup("\n".join(lines)),
            title="Configuration Error",
            border_style=theme.DANGER,
            expand=False,
        )
    )


def render_run_summary(
    console: Console,
    *,
    actions_executed: int,
    findings: int,
    hypotheses: int,
    tool_calls: int,
    violations: int,
) -> None:
    table = Table(show_header=False, box=None, padding=(0, 2, 0, 0))
    table.add_row("Findings", str(findings))
    table.add_row("Hypotheses", str(hypotheses))
    table.add_row("Tool Calls", str(tool_calls))
    style = theme.DANGER if violations else theme.SUCCESS
    table.add_row("Violations", Text(str(violations), style=style))
    table.add_row("Actions executed", str(actions_executed))
    console.print(table)


def elapsed_since(started_at: datetime) -> str:
    seconds = int((datetime.now(started_at.tzinfo) - started_at).total_seconds())
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


def render_engagement_panel(
    console: Console,
    *,
    title: str,
    name: str,
    scope: str,
    budget: str,
    focus: str | None = None,
) -> None:
    """Section 10/11: the one summary shown before Start, for both a
    brand-new draft and an existing config. Never shows a YAML key or
    internal id -- only what a human decided (target/scope/focus/budget).
    """
    lines = [f"[bold]{name}[/bold]", ""]
    lines.append(f"Scope    {scope}")
    if focus:
        lines.append(f"Focus    {focus}")
    lines.append(f"Budget   {budget}")
    console.print(
        Panel(
            Text.from_markup("\n".join(lines)),
            title=title,
            border_style=theme.EMPHASIS,
            expand=False,
        )
    )


def render_roe_checklist(
    console: Console, *, allowed: list[str], denied: list[str], documented_only: list[str]
) -> None:
    """Section 10: enforced allow/deny separated from prose-only notes,
    so a restriction the operator stated is never mistaken for one the
    Control Plane actually blocks (section 6 fail-closed philosophy)."""

    console.print("\n[bold]Rules of Engagement[/bold]\n")
    for item in allowed:
        console.print(f"  [{theme.SUCCESS}]{theme.ICON_SUCCESS}[/{theme.SUCCESS}] {item}")
    for item in denied:
        console.print(f"  [{theme.DANGER}]{theme.ICON_FAILURE}[/{theme.DANGER}] {item}")
    if not allowed and not denied:
        console.print(f"  [{theme.MUTED}](no capability-level restrictions set)[/{theme.MUTED}]")
    for note in documented_only:
        console.print(
            f"  [{theme.WARNING}]{theme.ICON_WARNING}[/{theme.WARNING}] {note} "
            f"[{theme.MUTED}](documented only, not enforced)[/{theme.MUTED}]"
        )


def render_runtime_checklist(console: Console, *, checks: list[tuple[str, bool, str]]) -> None:
    """``checks``: ``(label, ok, detail)``. section 20/21."""

    console.print("\n[bold]Runtime[/bold]\n")
    for label, ok, detail in checks:
        style = theme.SUCCESS if ok else theme.DANGER
        icon = theme.ICON_SUCCESS if ok else theme.ICON_FAILURE
        suffix = f"   {detail}" if detail else ""
        console.print(f"  [{style}]{icon}[/{style}] {label}{suffix}")


def render_capability_warning(console: Console) -> None:
    """Section 20: never let an empty ``capability.mcp_servers`` look
    like a fully-capable run -- same fact ``commands.run``'s own stderr
    warning states, surfaced where a human is actually looking (Preflight)."""

    console.print(
        f"\n  [{theme.WARNING}]{theme.ICON_WARNING}[/{theme.WARNING}] ExampleMCPAdapter\n"
        f"\n    [{theme.MUTED}]Development/test adapter only.\n"
        f"    Real tool execution is unavailable.[/{theme.MUTED}]"
    )


def prompt_choice(console: Console, prompt: str, options: str) -> str:
    console.print(f"\n{prompt}\n")
    console.print(f"  {options}")
    return console.input("\n> ").strip().lower()
