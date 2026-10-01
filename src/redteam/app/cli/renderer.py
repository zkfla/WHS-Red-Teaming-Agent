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
from redteam.app.cli.conversation import ConversationDecision
from redteam.core.contracts.events import EventEnvelope, EventType
from redteam.core.enums import ApprovalStatus
from redteam.orchestration.events import (
    AGENT_CONTEXT_PREPARED,
    AGENT_DECISION_RECORDED,
    AGENT_TURN_DECIDED,
    AUTHENTICATION_CHECK_DEFERRED,
    AUTHENTICATION_CHECKED,
    CANDIDATE_GENERATED,
    CANDIDATE_REJECTED,
    GRAPH_MUTATION_APPLIED,
    HYPOTHESIS_RESOLVED,
    MCP_TOOL_SELECTED,
    REPLAN_TRIGGERED,
    RESULT_MAPPER_FAILED,
)
from redteam.orchestration.operator_intents.models import (
    IntentApplication,
    IntentExplanation,
    IntentStatusReport,
)

# Events worth a line in the default (human-readable) stream. Same
# filtering intent as `commands.run._RENDERED_EVENTS`, kept separate
# since that module's own plain-line format is a documented, tested
# backward-compat surface this renderer must not touch (section 23).
# PlanningFailed is published by the planning node with payload
# {kind, stage, error_type} but has no shared constant yet (that module
# is owned elsewhere) -- the literal is kept here so a failed planning
# turn still gets an effective line instead of vanishing from the stream.
_PLANNING_FAILED = "PlanningFailed"


_RENDERED_EVENTS = frozenset(
    {
        EventType.RUN_STARTED,
        EventType.ACTION_PROPOSED,
        EventType.POLICY_EVALUATED,
        EventType.ACTION_APPROVED,
        EventType.ACTION_DENIED,
        EventType.APPROVAL_REQUIRED,
        EventType.TOOL_EXECUTION_FINISHED,
        EventType.TOOL_EXECUTION_FAILED,
        EventType.GRAPH_UPDATED,
        EventType.FINDING_CREATED,
        EventType.FINDING_VERIFIED,
        EventType.RUN_FINISHED,
        AGENT_CONTEXT_PREPARED,
        AGENT_DECISION_RECORDED,
        AGENT_TURN_DECIDED,
        CANDIDATE_GENERATED,
        CANDIDATE_REJECTED,
        GRAPH_MUTATION_APPLIED,
        HYPOTHESIS_RESOLVED,
        MCP_TOOL_SELECTED,
        REPLAN_TRIGGERED,
        RESULT_MAPPER_FAILED,
        AUTHENTICATION_CHECK_DEFERRED,
        AUTHENTICATION_CHECKED,
        _PLANNING_FAILED,
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


def _rejection_text(item: dict[str, Any]) -> str:
    operation = str(item.get("operation") or "unknown")
    reason = str(item.get("reason") or "unknown")
    field = item.get("field")
    suffix = f" ({field})" if field else ""
    return f"{operation}: {reason}{suffix}"


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
                + Text("[Action] ", style=theme.EMPHASIS)
                + Text(f"{agent}: {phrase}")
            )

        if event.event_type == AGENT_TURN_DECIDED:
            response = str(payload.get("response") or "다음 행동을 검토했습니다.")
            summary = str(payload.get("decision_summary") or "")
            assumptions = payload.get("assumptions") or []
            result = Text(f"[{timestamp}] ", style=theme.MUTED) + Text(
                f"[Agent] {response}", style=theme.EMPHASIS
            )
            if summary:
                result.append(f"\n           [Reason] {summary}", style=theme.MUTED)
            for assumption in assumptions[:3]:
                result.append(f"\n           [Assumption] {assumption}", style=theme.MUTED)
            return result

        if event.event_type == AGENT_CONTEXT_PREPARED:
            summary = str(payload.get("graph_summary") or "no graph summary")
            feedback = int(payload.get("runtime_observation_count") or 0)
            suffix = f"; {feedback} prior runtime observation(s)" if feedback else ""
            return Text(f"[{timestamp}] ", style=theme.MUTED) + Text(
                f"[Observation] {summary}{suffix}", style=theme.MUTED
            )

        if event.event_type == AGENT_DECISION_RECORDED:
            rationale = str(payload.get("rationale") or "selected from executable candidates")
            operation = str(payload.get("operation") or "next action")
            basis = str(payload.get("selection_basis") or "planner")
            return Text(f"[{timestamp}] ", style=theme.MUTED) + Text(
                f"[Agent] {rationale} -> {operation} ({basis})", style=theme.EMPHASIS
            )

        if event.event_type == MCP_TOOL_SELECTED:
            server = str(payload.get("server_id") or "mcp")
            tool = str(payload.get("tool_name") or payload.get("tool_id") or "tool")
            risk = str(payload.get("risk_level") or "unknown")
            health = str(payload.get("health_status") or "unknown")
            return Text(f"[{timestamp}] ", style=theme.MUTED) + Text(
                f"[MCP] {server}/{tool} selected (risk={risk}, health={health})",
                style=theme.MUTED,
            )

        if event.event_type == EventType.POLICY_EVALUATED:
            verdict = str(payload.get("verdict") or "unknown").upper()
            reasons = "; ".join(str(item) for item in payload.get("reasons", []) or [])
            rules = ", ".join(str(item) for item in payload.get("matched_rules", []) or [])
            detail = reasons or "no additional reason"
            if rules:
                detail += f"; policy={rules}"
            return Text(f"[{timestamp}] ", style=theme.MUTED) + Text(
                f"[Control] {verdict} — {detail}",
                style=(theme.SUCCESS if verdict == "ALLOW" else theme.WARNING),
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
            icon, style = (
                (theme.ICON_SUCCESS, theme.SUCCESS)
                if success
                else (
                    theme.ICON_FAILURE,
                    theme.DANGER,
                )
            )
            text = (
                "[Tool Result] Tool call succeeded" if success else "[Tool Result] Tool call failed"
            )
            return Text(f"[{timestamp}] ", style=theme.MUTED) + Text(f"{icon} {text}", style=style)

        if event.event_type == EventType.TOOL_EXECUTION_FAILED:
            error = payload.get("error_code")
            message = payload.get("error_message")
            detail = ": ".join(str(item) for item in (error, message) if item)
            suffix = f" ({detail})" if detail else ""
            return Text(f"[{timestamp}] ", style=theme.MUTED) + Text(
                f"{theme.ICON_FAILURE} [Tool Result] Tool call failed{suffix}", style=theme.DANGER
            )

        if event.event_type == EventType.GRAPH_UPDATED:
            node_type = str(payload.get("node_type", "node"))
            node_id = str(payload.get("node_id", ""))
            label = node_id.split(":", 1)[-1] if ":" in node_id else node_id
            return Text(f"[{timestamp}] ", style=theme.MUTED) + Text(
                f"{theme.ICON_TRANSITION} [State] Attack graph: {node_type} {label}",
                style=theme.MUTED,
            )

        if event.event_type == CANDIDATE_REJECTED:
            return Text(f"[{timestamp}] ", style=theme.MUTED) + Text(
                f"{theme.ICON_WARNING} Candidate rejected: {_rejection_text(payload)}",
                style=theme.WARNING,
            )

        if event.event_type == CANDIDATE_GENERATED:
            return Text(f"[{timestamp}] ", style=theme.MUTED) + Text(
                f"{theme.ICON_TRANSITION} Candidates generated: "
                f"{payload.get('candidate_count', 0)}",
                style=theme.MUTED,
            )

        if event.event_type == GRAPH_MUTATION_APPLIED:
            return Text(f"[{timestamp}] ", style=theme.MUTED) + Text(
                f"{theme.ICON_TRANSITION} [State] Graph mutation applied: "
                f"{payload.get('nodes', 0)} node(s), {payload.get('edges', 0)} edge(s)",
                style=theme.MUTED,
            )

        if event.event_type == HYPOTHESIS_RESOLVED:
            return Text(f"[{timestamp}] ", style=theme.MUTED) + Text(
                f"Hypothesis resolved: {payload.get('status', 'unknown')}",
                style=theme.MUTED,
            )

        if event.event_type == REPLAN_TRIGGERED:
            return Text(f"[{timestamp}] ", style=theme.MUTED) + Text(
                f"[Next] {payload.get('outcome', 'unknown')} ({payload.get('reason', 'unknown')})",
                style=theme.MUTED,
            )

        if event.event_type == RESULT_MAPPER_FAILED:
            return Text(f"[{timestamp}] ", style=theme.MUTED) + Text(
                f"{theme.ICON_WARNING} Result mapper failed: "
                f"{payload.get('operation', 'unknown')} / "
                f"{payload.get('mapper', 'unknown')} "
                f"({payload.get('reason', 'unknown')})",
                style=theme.WARNING,
            )

        if event.event_type in (EventType.FINDING_CREATED, EventType.FINDING_VERIFIED):
            title = str(payload.get("title") or payload.get("summary") or "New finding")
            return Text(f"[{timestamp}] ", style=theme.MUTED) + Text(
                f"{theme.ICON_WARNING} Finding: {title}", style=theme.WARNING
            )

        if event.event_type == AUTHENTICATION_CHECK_DEFERRED:
            return Text(f"[{timestamp}] Authentication check deferred: ") + Text(
                str(payload.get("reason", "unavailable")), style=theme.WARNING
            )
        if event.event_type == AUTHENTICATION_CHECKED:
            return Text(f"[{timestamp}] Authentication observation: ") + Text(
                str(payload.get("outcome", "inconclusive")), style=theme.MUTED
            )

        if event.event_type == _PLANNING_FAILED:
            kind = str(payload.get("kind") or "unknown")
            stage = str(payload.get("stage") or "unknown")
            error_type = str(payload.get("error_type") or "unknown")
            return Text(f"[{timestamp}] ", style=theme.MUTED) + Text(
                f"{theme.ICON_WARNING} Planning failed: kind={kind} stage={stage} ({error_type})",
                style=theme.WARNING,
            )

        if event.event_type == EventType.RUN_FINISHED:
            reason = str(payload.get("termination_reason") or "completed")
            return Text(f"[{timestamp}] ", style=theme.MUTED) + Text(
                f"[Agent] Waiting — run ended: {reason}", style=theme.MUTED
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


def render_user_message(console: Console, text: str) -> None:
    """Render a chat turn without surrounding it in noisy terminal chrome."""

    console.print()
    console.print(Text.assemble(("› ", f"bold {theme.EMPHASIS}"), text))


def render_conversation_decision(console: Console, decision: ConversationDecision) -> None:
    """Codex-style public reasoning summary: response, assumptions and plan.

    These fields are explicitly requested as short user-facing summaries;
    they are not model chain-of-thought.
    Single LLM-path flow only: no fallback-recipe line is ever shown.
    """

    body = Text(decision.response)
    if decision.assumptions:
        body.append("\n\nAssumptions\n", style=f"bold {theme.WARNING}")
        for item in decision.assumptions:
            body.append(f"  • {item}\n", style=theme.MUTED)
    if decision.plan:
        body.append("\nPlan\n", style=f"bold {theme.EMPHASIS}")
        for index, item in enumerate(decision.plan, start=1):
            body.append(f"  {index}. {item}\n", style=theme.MUTED)
    console.print(Panel(body, title="Assistant", border_style=theme.EMPHASIS, padding=(1, 2)))


def render_assistant_decision(console: Console, decision: ConversationDecision) -> None:
    """Backward-compatible alias of render_conversation_decision."""

    render_conversation_decision(console, decision)


def render_final_answer(console: Console, text: str) -> None:
    console.print(
        Panel(
            Text(text),
            title="Result",
            border_style=theme.SUCCESS,
            padding=(1, 2),
        )
    )


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


# --- Operator console (PROMPT 4+5: persistent runtime input) --------------
#
# These helpers return *plain* strings on purpose. They are printed through
# the same ``echo`` sink ``commands.run.execute`` already threads through to
# both renderers (Rich ``console.print`` in the interactive entrypoint,
# ``print`` in ``redteam run``), so they may not contain Rich-style markup
# tags (the plain path would print them literally). One line per call keeps
# operator/event output serialized on the single asyncio loop.


def operator_welcome_text(*, live: bool = False) -> str:
    if live:
        return (
            "operator> live input enabled -- type 'help' for "
            "pause/resume/stop/focus/exclude, 'status' or 'explain' to inspect, "
            "and 'approve' / 'deny' when an action is pending."
        )
    return (
        "operator> no interactive input on this stdin -- this run cannot be "
        "steered here. Resolve approvals from another terminal with "
        "'redteam approvals approve|deny <id>'."
    )


def operator_user_text(text: str) -> str:
    """Echo accepted input in the same trace vocabulary as runtime events."""

    return f"[User] {text}"


def operator_assistant_text(decision: ConversationDecision) -> str:
    """Plain serialization for the live input channel's shared echo sink."""

    lines = [f"[Assistant] {decision.response}"]
    lines.extend(f"  assumption: {item}" for item in decision.assumptions)
    lines.extend(f"  plan {index}: {item}" for index, item in enumerate(decision.plan, start=1))
    return "\n".join(lines)


def operator_help_text() -> str:
    return (
        "operator console commands:\n"
        "  pause                 pause the mission (active task preserved)\n"
        "  resume                resume a paused mission\n"
        "  stop                  stop the mission and cancel open tasks\n"
        "  focus AREA            raise priority of matching tasks\n"
        "  exclude AREA          cancel matching non-running tasks\n"
        "  reprioritize AREA N   set priority N on matching tasks\n"
        "  status | query        show mission/task status\n"
        "  explain               explain the current position\n"
        "  try another method    add strategy guidance to the next decision\n"
        "  approve ID            approve the pending action (in-session)\n"
        "  deny ID               deny the pending action (in-session)\n"
        "  help                  show this help\n"
        "Areas: recon, web, api, authentication, authorization, mobile"
    )


def operator_intent_text(application: IntentApplication) -> str:
    if not application.applied:
        reason = application.reason or application.outcome or "rejected"
        return f"operator> intent {application.status.value}: {reason}"
    intent_type = application.intent_type.value if application.intent_type else "intent"
    affected = len(application.affected_task_ids)
    suffix = f" ({affected} task(s))" if affected else ""
    return f"operator> {intent_type}: {application.outcome}{suffix}"


def operator_status_text(report: IntentStatusReport) -> str:
    counts = ", ".join(
        f"{name} {count}" for name, count in sorted(report.task_status_counts.items())
    )
    lines = [
        f"operator> mission {report.mission_status.value}; "
        f"{report.task_count} task(s)"
        + (f" ({counts})" if counts else "")
        + (" (paused)" if report.paused else "")
    ]
    if report.focus_areas:
        lines.append(f"  focus: {', '.join(report.focus_areas)}")
    if report.excluded_areas:
        lines.append(f"  excluded: {', '.join(report.excluded_areas)}")
    if report.excluded_endpoints:
        lines.append(f"  excluded endpoints: {', '.join(report.excluded_endpoints[:3])}")
    for task in report.blocked_tasks[:3]:
        lines.append(f"  blocked {task.task_id}: {task.blocking_reason}")
    for entry in report.recent_intents[-3:]:
        lines.append(
            f"  - {entry.intent_type.value}: {entry.status.value}"
            + (f" ({entry.outcome})" if entry.outcome else "")
        )
    return "\n".join(lines)


def operator_explanation_text(explanation: IntentExplanation) -> str:
    lines = [f"operator> {explanation.summary}"]
    if explanation.paused:
        lines.append("  (paused -- use 'resume' to continue the same task)")
    if explanation.active_task_title:
        lines.append(f"  active task: {explanation.active_task_title}")
    top = sorted(explanation.task_priorities.items(), key=lambda item: -item[1])[:3]
    if top:
        lines.append("  top priorities: " + ", ".join(f"{tid}={prio}" for tid, prio in top))
    for task in explanation.blocked_tasks[:3]:
        deps = ", ".join(str(item) for item in task.blocked_dependency_ids[:3])
        lines.append(
            f"  blocked {task.task_id}: {task.blocking_reason}"
            + (f" (waiting on {deps})" if deps else "")
        )
    for candidate in explanation.rejected_candidates[:3]:
        score = f" (score={candidate.priority:g})" if candidate.priority is not None else ""
        lines.append(
            f"  rejected {candidate.operation}{score}: {candidate.reason or 'unspecified'}"
        )
    for action in explanation.chosen_actions[:3]:
        score = f" score={action.priority:g}" if action.priority is not None else ""
        policy = f" policy={action.policy_verdict}" if action.policy_verdict else ""
        reason = f" ({action.policy_reasons[0]})" if action.policy_reasons else ""
        lines.append(
            f"  chosen {action.operation}: {action.status or 'proposed'}{score}{policy}{reason}"
        )
    return "\n".join(lines)


def operator_approval_prompt_text(approval_id: str) -> str:
    return (
        f"[Assistant] 이 Action은 실행 전에 승인이 필요합니다 ({approval_id}). "
        "실행할까요? approve = 이 Action만 허용 / deny = 거부 후 다른 전략 검토"
    )


def operator_approval_pending_text(approval_id: str, run_id: str) -> str:
    return (
        f"operator> approval {approval_id} pending for run {run_id} -- resolve with "
        f"'redteam approvals approve|deny {approval_id}', then "
        f"'redteam resume {run_id} <engagement.yaml>'."
    )


def operator_paused_text(run_id: str, *, live: bool = False) -> str:
    if live:
        return (
            f"operator> run {run_id} paused -- type 'resume' to continue the "
            f"same Mission, or 'stop'."
        )
    return (
        f"operator> run {run_id} paused -- no interactive input here; resume "
        f"from an interactive session to continue the same Mission."
    )


def operator_approval_resolved_text(approval_id: str, status: ApprovalStatus) -> str:
    return f"operator> approval {approval_id} -> {status.value}"


def operator_notice_text(message: str) -> str:
    return f"operator> {message}"


def prompt_choice(console: Console, prompt: str, options: str, *, normalize: bool = True) -> str:
    console.print(f"\n{prompt}\n")
    console.print(f"  {options}")
    answer = console.input("\n> ").strip()
    return answer.lower() if normalize else answer
