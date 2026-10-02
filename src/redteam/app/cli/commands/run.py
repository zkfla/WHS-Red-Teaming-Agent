"""``redteam run`` — the only command that executes an Engagement.

Canonical path, no CLI-only shortcut:

    engagement.yaml + environment (REDTEAM_LLM_*/REDTEAM_MCP_*) -> AppConfig
        -> app.bootstrap.build_application(config)   (real LLM, real MCP, real Control)
        -> app.bootstrap.run_engagement(engagement, deps=deps)
        -> Orchestration -> ActionProposal -> Control -> Tool Selection
        -> ToolGateway -> MCP Adapter

This module never decides ROE/Scope/Tool-selection/Approval policy — it
only builds the config, starts the run, prints the events it's handed,
and (interactive or plain CLI external-approval-watch mode) polls for a resolved Approval using
``AgentLoopRunner.reconcile_once`` — the same reconciliation primitive
built for exactly this, not a bespoke wait loop.

``execute()`` below is the shared core (build -> subscribe -> run ->
approval wait) also used by the default ``redteam`` interactive
entrypoint (``commands.interactive``) — only the event renderer and the
handful of plain-text lines (``echo``) differ between the two; the
call sequence, exception types, and this command's own plain
``[EventType] k=v``/``--json`` output are unchanged from before that
split (section 23: this explicit command stays script/CI-safe).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import sys
from collections.abc import Callable
from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

if TYPE_CHECKING:
    from prompt_toolkit import PromptSession

from redteam.app.bootstrap import build_application, run_engagement, settle_engagement
from redteam.app.cli import renderer
from redteam.app.cli._shared import (
    EXIT_CONFIG_ERROR,
    EXIT_FAILURE,
    EXIT_OK,
    eprint,
    load_app_config_for_run,
    mask_secrets,
)
from redteam.app.cli.conversation import ConversationAssistant, catalog_tool_labels
from redteam.capability.settings import MCPConfigurationError
from redteam.core.config import AppConfig
from redteam.core.contracts.events import EventEnvelope, EventType
from redteam.core.enums import ApprovalStatus, OperatorIntentType
from redteam.core.ids import EngagementId, MissionId, RunId
from redteam.core.ports.event_bus import EventHandler
from redteam.event_bus.publisher import EventPublisher
from redteam.orchestration.component_binding import ComponentProfile, ComponentRegistry
from redteam.orchestration.context import RuntimeDeps
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
from redteam.orchestration.operator_intents import OperatorIntentService, redact_text
from redteam.orchestration.runner import AgentLoopRunner
from redteam.orchestration.settings import OrchestrationSettings
from redteam.orchestration.state import RuntimeState, TerminationReason

Echo = Callable[[str], None]

# Events worth a human's/CI's attention; TOOL_EXECUTION_STARTED and
# AGENT_STARTED/FINISHED are structural noise for a single-line stream.
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
        EventType.EVIDENCE_CREATED,
        EventType.FINDING_CREATED,
        EventType.FINDING_VERIFIED,
        EventType.GRAPH_UPDATED,
        EventType.RUN_FINISHED,
        # Planning failures (compile failure / budget exhaustion) are the
        # production signal for "nothing executable came out of planning".
        _PLANNING_FAILED,
        AGENT_TURN_DECIDED,
        CANDIDATE_GENERATED,
        CANDIDATE_REJECTED,
        GRAPH_MUTATION_APPLIED,
        HYPOTHESIS_RESOLVED,
        REPLAN_TRIGGERED,
        RESULT_MAPPER_FAILED,
        AUTHENTICATION_CHECK_DEFERRED,
        AUTHENTICATION_CHECKED,
        MCP_TOOL_SELECTED,
        AGENT_CONTEXT_PREPARED,
        AGENT_DECISION_RECORDED,
    }
)

#: How often the interactive wait re-checks whether a pending Approval
#: has been resolved -- also the cap on how stale "still waiting" looks.
_APPROVAL_POLL_SECONDS = 5.0
#: When an in-session operator console is live, approve/deny should resume
#: the run promptly instead of waiting out the full cross-terminal poll.
_APPROVAL_POLL_SECONDS_IN_SESSION = 1.0


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("run", help="Run an Engagement end-to-end")
    parser.add_argument("engagement_path")
    parser.add_argument("--non-interactive", action="store_true")
    parser.add_argument("--max-steps", type=int, default=None)
    parser.add_argument("--log-level", default=None)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--objective", default=None, help="Natural-language Mission objective")
    parser.set_defaults(handler=run)


def _event_subscriber(*, json_mode: bool):  # noqa: ANN202
    async def _handler(event: EventEnvelope) -> None:
        _render_event(event, json_mode=json_mode)

    return _handler


def _render_event(event: EventEnvelope, *, json_mode: bool) -> None:
    if json_mode:
        # JSON Lines: one compact object per event, matching this
        # project's own evidence/audit `.jsonl` convention. Not a single
        # JSON document, because `run` streams rather than batching.
        print(json.dumps(mask_secrets(event.model_dump(mode="json")), separators=(",", ":")))
        return
    if event.event_type not in _RENDERED_EVENTS:
        return
    if event.event_type == CANDIDATE_REJECTED:
        payload = event.payload
        field = f" field={payload['field']}" if payload.get("field") else ""
        print(
            "candidate rejected: "
            f"operation={payload.get('operation', 'unknown')} "
            f"reason={payload.get('reason', 'unknown')}{field}"
        )
        return
    if event.event_type == RESULT_MAPPER_FAILED:
        payload = event.payload
        print(
            "result mapper failed: "
            f"operation={payload.get('operation', 'unknown')} "
            f"mapper={payload.get('mapper', 'unknown')} "
            f"reason={payload.get('reason', 'unknown')}"
        )
        return
    if event.event_type == _PLANNING_FAILED:
        payload = event.payload
        print(
            "planning failed: "
            f"kind={payload.get('kind', 'unknown')} "
            f"stage={payload.get('stage', 'unknown')} "
            f"error={payload.get('error_type', 'unknown')}"
        )
        return
    if event.event_type == AGENT_TURN_DECIDED:
        payload = mask_secrets(event.payload)
        print(f"[Agent] {payload.get('response', '')}")
        if payload.get("decision_summary"):
            print(f"[Reason] {payload['decision_summary']}")
        for item in (payload.get("assumptions") or [])[:3]:
            print(f"[Assumption] {item}")
        return
    fields = " ".join(f"{k}={v}" for k, v in mask_secrets(event.payload).items() if v is not None)
    print(f"[{event.event_type}] {fields}")


# --- Operator console (PROMPT 4+5) ----------------------------------------
#
# A run is a long-lived single asyncio process. The operator needs to keep
# typing while it runs -- pause/resume/stop/focus/exclude/status and, when
# the run suspends on an Approval, approve/deny *in the same session* --
# without a Textual rewrite and without a cursor fight with the event
# renderer.
#
# Mechanism, deliberately dependency-free:
#   * one daemon thread blocks on ``sys.stdin.readline`` and hands each line
#     to the loop with ``call_soon_threadsafe`` (the only thread-safe way to
#     touch an ``asyncio.Queue``);
#   * one consumer task, on the same loop as the event renderer, drains that
#     queue and dispatches each line. PromptToolkit redraws the user's
#     unfinished input after event output, preserving cursor and focus.
#
# The channel routes through ``OperatorIntentService`` (never a tool, never
# the Control Plane's policy path): the same service OBSERVE already drains,
# so PAUSE/STOP take effect before the next scheduled action. Approval
# resolution deliberately calls ``DefaultControlPlane.resolve_approval`` --
# the *same* Approval the runner is already reconciling against -- and never
# replans or mints a new proposal.

_APPROVE_COMMANDS = frozenset({"approve", "a", "y", "yes"})
_DENY_COMMANDS = frozenset({"deny", "d", "n", "no", "reject"})
_QUERY_COMMANDS = frozenset({"status", "query", "현황", "상태", "진행", "요약"})
_EXPLAIN_COMMANDS = frozenset({"explain", "why", "설명", "이유", "왜"})
_HELP_COMMANDS = frozenset({"help", "?", "h"})


def _default_resolved_by() -> str:
    return os.environ.get("USER") or os.environ.get("USERNAME") or "cli-operator"


def _stdin_is_interactive() -> bool:
    stream = sys.stdin
    if stream is None or getattr(stream, "closed", False):
        return False
    isatty = getattr(stream, "isatty", None)
    return bool(isatty()) if callable(isatty) else False


@dataclass
class RuntimeIdentity:
    """Run/engagement/mission ids learned from the live event stream.

    ``execute`` cannot know the run/mission before ``run_engagement`` mints
    them, so the wrapped subscriber records them off the envelopes it is
    already delivering. The operator channel answers "mission still
    starting" until the mission id has been seen.
    """

    run_id: RunId | None = None
    engagement_id: EngagementId | None = None
    mission_id: MissionId | None = None

    def observe(self, event: EventEnvelope) -> None:
        self.run_id = event.run_id
        self.engagement_id = event.engagement_id
        if event.event_type == EventType.MISSION_CREATED:
            mission_id = event.payload.get("mission_id")
            if mission_id is not None:
                self.mission_id = MissionId(str(mission_id))


class OperatorConsole:
    """Persistent, in-session operator input for one run.

    Construction is cheap and side-effect free (so it is unit-testable
    without a thread or a loop): call :meth:`start` to attach the reader
    thread + consumer task, and :meth:`handle_line` directly from a test.
    """

    def __init__(
        self,
        deps: RuntimeDeps,
        echo: Echo,
        *,
        identity: RuntimeIdentity,
        resolved_by: str | None = None,
        conversation: ConversationAssistant | None = None,
        engagement_name: str = "active engagement",
        scope: list[str] | None = None,
    ) -> None:
        self._deps = deps
        self._echo = echo
        self._identity = identity
        self._resolved_by = resolved_by or _default_resolved_by()
        self._service = OperatorIntentService.from_deps(deps)
        self._conversation = conversation
        self._engagement_name = engagement_name
        self._scope = list(scope or [])
        self._pending_approval_id: str | None = None
        self._queue: asyncio.Queue[str] | None = None
        self._consumer: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        # Live-input plumbing. Declared up front (not only assigned in
        # ``start``) so the attribute types are stable and ``input_available``
        # is answerable before the console is ever started.
        self._input_live = False
        self._prompt_session: PromptSession[str] | None = None
        self._prompt_context: Any | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._fallback_loop: asyncio.AbstractEventLoop | None = None
        self._fallback_fd: int | None = None

    @property
    def input_available(self) -> bool:
        """Whether a live, in-session input channel is actually attached.

        False for ``--non-interactive``/``--json`` and for any non-TTY stdin
        (a pipe/CI), where claiming "live input" or blocking at an Approval
        would be a lie/hang.
        """
        return self._input_live and not self._stop.is_set()

    # -- lifecycle ------------------------------------------------------

    def start(self) -> None:
        loop = asyncio.get_running_loop()
        self._queue = asyncio.Queue()
        self._consumer = loop.create_task(self._consume())
        if not _stdin_is_interactive():
            # Script/CI or a redirected stdin: there is no operator to read
            # from. The consumer stays attached (the run still needs the
            # event observer), but nothing may claim live input.
            return
        # PromptToolkit preserves the line being edited (and its cursor)
        # while Rich streams event output from the same event loop. It is an
        # optional-at-runtime convenience: if it cannot attach, the run must
        # still proceed with input marked unavailable rather than crash.
        try:
            from prompt_toolkit import PromptSession
            from prompt_toolkit.patch_stdout import patch_stdout
        except Exception as exc:  # noqa: BLE001 - never let input setup kill the run
            logging.getLogger(__name__).debug(
                "PromptToolkit unavailable; using the basic terminal reader",
                exc_info=exc,
            )
            self._start_basic_reader(loop)
            return
        try:
            # `raw=True`: every event line printed here comes from our own
            # Rich renderer, never from untrusted/user-controlled text, so
            # it is safe to let its ANSI codes through unmangled.
            # `patch_stdout()`'s default (`raw=False`) calls
            # `Output.write()`, whose vt100/Windows10 backends do
            # ``data.replace("\x1b", "?")`` to neutralize *arbitrary*
            # escape sequences -- turning every `\x1b[32m` into a literal
            # `?[32m` on screen. macOS/Linux terminals happened to still
            # look fine whenever stdin wasn't a live TTY (patch_stdout
            # never engaged at all, so nothing was ever mangled there);
            # Windows makes the corruption visible on any real terminal
            # session. `raw=True` uses `Output.write_raw()` instead, which
            # passes escape sequences straight through on every platform.
            self._prompt_context = patch_stdout(raw=True)
            self._prompt_context.__enter__()
            self._prompt_session = PromptSession()
            self._input_live = True
            self._reader_task = loop.create_task(self._read_prompt())
        except Exception as exc:  # noqa: BLE001 - same fail-safe contract
            self._input_live = False
            if self._prompt_context is not None:
                self._prompt_context.__exit__(None, None, None)
                self._prompt_context = None
            logging.getLogger(__name__).debug(
                "PromptToolkit could not attach; using the basic terminal reader",
                exc_info=exc,
            )
            self._start_basic_reader(loop)

    def _start_basic_reader(self, loop: asyncio.AbstractEventLoop) -> None:
        """Attach a dependency-free TTY reader when PromptToolkit is absent.

        POSIX ``add_reader`` is cancellable, so handing stdin back to the
        post-run session prompt cannot lose a line to a blocked worker. A
        background ``readline`` thread is deliberately not used when fd
        readers are unavailable: it cannot be cancelled safely and could
        steal the first line entered at the post-run prompt.
        """

        try:
            fd = sys.stdin.fileno()
            loop.add_reader(fd, self._read_stdin_ready)
        except (AttributeError, NotImplementedError, OSError, RuntimeError, ValueError) as exc:
            self._input_live = False
            self._echo(
                renderer.operator_notice_text(
                    f"live operator input unavailable ({type(exc).__name__}); "
                    "resolve approvals from another terminal"
                )
            )
            return
        self._fallback_loop = loop
        self._fallback_fd = fd
        self._input_live = True

    def _read_stdin_ready(self) -> None:
        if self._stop.is_set():
            return
        line = sys.stdin.readline()
        if not line:
            self._input_live = False
            return
        if self._queue is not None:
            self._queue.put_nowait(line.rstrip("\r\n"))

    async def _read_prompt(self) -> None:
        try:
            while not self._stop.is_set():
                session = self._prompt_session
                if session is None:
                    return
                try:
                    line = await session.prompt_async("operator> ")
                except (EOFError, KeyboardInterrupt):
                    return
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - surface, don't die silently
                    # A dead reader must be visible: otherwise the console
                    # keeps looking alive while silently dropping all input.
                    self._echo(
                        renderer.operator_notice_text(
                            f"operator input stopped: {redact_text(str(exc))}; "
                            "resolve approvals from another terminal"
                        )
                    )
                    return
                if self._queue is not None:
                    self._queue.put_nowait(line)
        finally:
            self._input_live = False

    async def aclose(self) -> None:
        self._stop.set()
        self._input_live = False
        fallback_loop = self._fallback_loop
        fallback_fd = self._fallback_fd
        self._fallback_loop = None
        self._fallback_fd = None
        if fallback_loop is not None and fallback_fd is not None:
            fallback_loop.remove_reader(fallback_fd)
        reader = self._reader_task
        self._reader_task = None
        if reader is not None:
            reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await reader
        prompt_context = self._prompt_context
        self._prompt_context = None
        if prompt_context is not None:
            prompt_context.__exit__(None, None, None)
        consumer = self._consumer
        self._consumer = None
        if consumer is not None:
            consumer.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await consumer

    async def _consume(self) -> None:
        assert self._queue is not None
        while True:
            line = await self._queue.get()
            try:
                await self.handle_line(line)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - one bad command must not kill the run
                self._echo(
                    renderer.operator_notice_text(f"command failed: {redact_text(str(exc))}")
                )

    # -- approval identity ---------------------------------------------

    def set_pending_approval(self, approval_id: object | None) -> None:
        self._pending_approval_id = str(approval_id) if approval_id else None

    def clear_pending_approval(self) -> None:
        self._pending_approval_id = None

    # -- dispatch -------------------------------------------------------

    async def handle_line(self, text: str) -> None:
        text = (text or "").strip()
        if not text:
            return
        self._echo(renderer.operator_user_text(redact_text(text)))
        head, _, rest = text.partition(" ")
        # Natural-language punctuation ("status?", "pause!") must not defeat
        # the console aliases. Anything that is *not* a known console command
        # is handed to the interpreter as the full sentence, so multi-word
        # English/Korean phrasing routes the same as the bare keyword. A lone
        # "?" is itself the help alias, so only strip when something remains.
        command = head.lower()
        stripped = command.strip("?!.,:;")
        if stripped:
            command = stripped

        if command in _HELP_COMMANDS:
            self._echo(renderer.operator_help_text())
            return
        if command in _APPROVE_COMMANDS:
            await self._resolve_approval(ApprovalStatus.APPROVED, rest.strip())
            return
        if command in _DENY_COMMANDS:
            await self._resolve_approval(ApprovalStatus.REJECTED, rest.strip())
            return
        if command in _QUERY_COMMANDS:
            # "query" is a CLI alias; the interpreter's own status vocabulary
            # is used for everything else so Korean phrasing keeps working.
            await self._apply_intent("status" if command == "query" else text)
            return
        if command in _EXPLAIN_COMMANDS:
            await self._apply_intent(text)
            return
        await self._apply_intent(text)

    # -- intent path (no tool, no policy) -------------------------------

    async def _apply_intent(self, text: str) -> None:
        from redteam.app.cli.input_router import command

        if self._conversation is not None and command(text) is None:
            await self._handle_chat_fallback(text)
            return
        if self._service is None:
            self._echo(
                renderer.operator_notice_text("operator intent channel is not wired in this run")
            )
            return
        if self._identity.mission_id is None:
            self._echo(
                renderer.operator_notice_text("mission is still starting; try again shortly")
            )
            return

        application = await self._service.receive_and_apply(
            text,
            mission_id=self._identity.mission_id,
            run_id=self._identity.run_id,
            publisher=self._publisher(),
            allow_guidance=True,
        )
        if not application.applied and self._conversation is not None:
            await self._handle_chat_fallback(text)
            return

        self._echo(renderer.operator_intent_text(application))

        if application.intent_type is OperatorIntentType.STATUS_QUERY:
            await self._show_status()
        elif application.intent_type is OperatorIntentType.EXPLANATION_QUERY:
            await self._show_explanation()

    async def _handle_chat_fallback(self, text: str) -> None:
        """Let the configured LLM answer or turn arbitrary chat into guidance.

        The returned text never selects or invokes a tool. Execution-shaped
        replies are persisted only as Mission guidance and therefore still
        pass through OBSERVE -> PLAN -> compiler -> Control -> MCP.
        """

        service = self._service
        mission_id = self._identity.mission_id
        conversation = self._conversation
        if service is None or mission_id is None or conversation is None:
            return
        status = await service.status(mission_id=mission_id)
        explanation = await service.explain(mission_id=mission_id, run_id=self._identity.run_id)
        try:
            decision = await conversation.decide(
                text,
                engagement_name=self._engagement_name,
                scope=self._scope,
                tools=catalog_tool_labels(self._deps.tool_catalog),
                session_context=f"{status.summary} {explanation.summary}",
                run_active=True,
            )
        except Exception as exc:  # noqa: BLE001 - chat is advisory; the active run continues
            logging.getLogger(__name__).warning(
                "Operator chat interpretation failed (%s)", type(exc).__name__
            )
            self._echo(
                renderer.operator_notice_text(
                    "could not interpret that as guidance; the run continues"
                )
            )
            return
        self._echo(renderer.operator_assistant_text(decision))
        if decision.mode != "guidance":
            if decision.mode == "execute":
                self._echo(
                    "새 조사 요청은 현재 Mission 지시로 전달하지 않습니다. "
                    "대화 세션에서 시작하세요."
                )
            return
        guidance = decision.guidance or decision.objective or text
        # Frozen explicit-guidance entry point (W4): records the raw
        # instruction as GUIDANCE without keyword reclassification.
        guided = await service.receive_guidance_and_apply(
            guidance,
            mission_id=mission_id,
            run_id=self._identity.run_id,
            publisher=self._publisher(),
            source="operator_chat",
        )
        self._echo(renderer.operator_intent_text(guided))

    async def _show_status(self) -> None:
        service = self._service
        mission_id = self._identity.mission_id
        if service is None or mission_id is None:
            return
        report = await service.status(mission_id=mission_id)
        self._echo(renderer.operator_status_text(report))

    async def session_context(self) -> str:
        """Read-only context for CLI classification; never writes a user intent."""
        if self._service is None or self._identity.mission_id is None:
            return "실행 채널 준비 중"
        report = await self._service.status(mission_id=self._identity.mission_id)
        explanation = await self._service.explain(
            mission_id=self._identity.mission_id, run_id=self._identity.run_id
        )
        return (
            renderer.operator_status_text(report)
            + "\n"
            + renderer.operator_explanation_text(explanation)
        )

    async def dispatch(self, kind: str, value: str) -> tuple[bool, str]:
        """Already-routed requests only. Errors propagate as uncertain receipts.

        The session owns stdin and mutation task lifetimes. This adapter only
        calls the existing application services, not tools or policy evaluation.
        """
        if kind in {"approve", "deny"}:
            if not value or value != self._pending_approval_id:
                return False, "승인 대상이 변경되었거나 없습니다."
            resolver = getattr(self._deps.control_plane, "resolve_approval", None)
            if resolver is None:
                return False, "현재 Control Plane은 세션 내 승인 처리를 지원하지 않습니다."
            status = ApprovalStatus.APPROVED if kind == "approve" else ApprovalStatus.REJECTED
            result = await resolver(value, status, self._resolved_by)
            if str(result.approval_id) != value or result.status != status:
                raise RuntimeError("approval response did not acknowledge this request")
            self.clear_pending_approval()
            return (
                True,
                f"승인 결정 수락: {value} · {status.value} — action 실행 성공과 별개입니다.",
            )
        if self._service is None or self._identity.mission_id is None:
            return False, "현재 Mission 입력 채널이 없습니다."
        kwargs: dict[str, Any] = dict(
            mission_id=self._identity.mission_id,
            run_id=self._identity.run_id,
            publisher=self._publisher(),
        )
        if kind == "guidance":
            application = await self._service.receive_guidance_and_apply(
                value, **kwargs, source="operator_chat"
            )
        else:
            application = await self._service.receive_and_apply(
                (kind + " " + value).strip(), **kwargs, allow_guidance=False
            )
        return application.applied, (
            f"{application.outcome} — 지시 수락과 Agent의 실제 반영은 별개입니다."
            if application.applied
            else str(application.outcome)
        )

    async def _show_explanation(self) -> None:
        service = self._service
        mission_id = self._identity.mission_id
        if service is None or mission_id is None:
            return
        explanation = await service.explain(mission_id=mission_id, run_id=self._identity.run_id)
        self._echo(renderer.operator_explanation_text(explanation))

    def _publisher(self) -> EventPublisher | None:
        if self._identity.run_id is None or self._identity.engagement_id is None:
            return None
        return EventPublisher(
            self._deps.event_bus,
            run_id=self._identity.run_id,
            engagement_id=self._identity.engagement_id,
            source="cli.operator",
        )

    # -- approval path (same DefaultControlPlane resolution) ------------

    async def _resolve_approval(self, status: ApprovalStatus, explicit_id: str) -> None:
        pending = self._pending_approval_id
        if not pending:
            self._echo(renderer.operator_notice_text("no action is awaiting approval"))
            return
        if explicit_id and explicit_id != pending:
            # Never resolve a different Approval than the one this run is
            # actually suspended on -- that would break the resume identity.
            self._echo(
                renderer.operator_notice_text(
                    f"approval '{explicit_id}' does not match the pending approval '{pending}'"
                )
            )
            return

        resolver = getattr(self._deps.control_plane, "resolve_approval", None)
        if resolver is None:
            self._echo(
                renderer.operator_notice_text(
                    "this control plane cannot resolve approvals in-session; "
                    f"use: redteam approvals approve|deny {pending}"
                )
            )
            return
        try:
            await resolver(pending, status, self._resolved_by)
        except Exception as exc:  # noqa: BLE001 - report, keep the run suspended
            self._echo(
                renderer.operator_notice_text(
                    f"could not resolve approval {pending}: {redact_text(str(exc))}"
                )
            )
            return
        self._pending_approval_id = None
        self._echo(renderer.operator_approval_resolved_text(pending, status))


async def build(
    config: AppConfig,
    *,
    max_steps: int | None,
    mcp_stack: AsyncExitStack | None = None,
    component_profile: ComponentProfile | None = None,
    component_registry: ComponentRegistry | None = None,
) -> tuple[Any, RuntimeDeps]:
    """Phase 1 of the shared core (section 17): config -> live
    ``(Engagement, RuntimeDeps)``, via the canonical
    ``app.bootstrap.build_application``. Kept separate from
    :func:`execute` so a caller can give this phase's failures a
    different message than a failure once the run is actually
    underway -- exactly what ``run()`` below does, unchanged from
    before this was split out.
    """
    if not config.capability.mcp_servers:
        eprint(
            "WARNING: no MCP servers configured -- this run has an empty tool catalog "
            "and cannot execute any Action. Set REDTEAM_MCP_SERVERS (comma-separated "
            "server ids) and each server's REDTEAM_MCP_<ID>_* environment variables."
        )

    orchestration_settings = OrchestrationSettings.for_durable_run(
        config.orchestration, max_steps=max_steps
    )

    # Omit unset selections so the composition root uses its builtin profile.
    component_options: dict[str, Any] = {}
    if component_profile is not None:
        component_options["component_profile"] = component_profile
    if component_registry is not None:
        component_options["component_registry"] = component_registry
    return await build_application(
        config,
        orchestration_settings=orchestration_settings,
        mcp_stack=mcp_stack,
        **component_options,
    )


async def execute(
    engagement: Any,
    deps: RuntimeDeps,
    *,
    config: AppConfig,
    non_interactive: bool,
    on_event: EventHandler,
    echo: Echo,
    objective: str | None = None,
    allow_operator_input: bool = False,
    wait_for_external_approval: bool = False,
    conversation: ConversationAssistant | None = None,
    session_channel: Any | None = None,
) -> RuntimeState:
    """Phase 2 of the shared core: subscribe -> run -> approval wait.
    Callers own exception handling/exit codes and choose the renderer
    (``on_event``) and plain-text sink (``echo``) -- this function makes
    neither decision itself. Used by both this module's own ``run()``
    (plain renderer, ``echo=print``) and ``commands.interactive`` (Rich
    renderer/echo).

    ``allow_operator_input`` (interactive sessions only; never
    ``--non-interactive``/``--json``) attaches a persistent operator
    channel for the whole run, including the approval suspension -- see
    ``OperatorConsole``. Defaults off so every existing caller is
    unchanged and script/CI invocations never block on stdin.

    ``wait_for_external_approval`` preserves the plain CLI's original
    approval-watch behavior when stdin is redirected: a separate approval CLI
    can resolve the durable Approval while this process watches. It is opt-in
    so direct/programmatic callers and JSON/non-interactive runs still return
    a pending checkpoint instead of waiting without a known operator channel.
    """
    identity = RuntimeIdentity()
    operator_console: OperatorConsole | None = None

    if allow_operator_input or session_channel is not None:
        engagement_scope = getattr(engagement, "scope", None)
        scope_values = (
            [
                *engagement_scope.allowed_hosts,
                *engagement_scope.allowed_domains,
                *engagement_scope.allowed_networks,
                *engagement_scope.allowed_assets,
            ]
            if engagement_scope is not None
            else []
        )
        operator_console = OperatorConsole(
            deps,
            echo,
            identity=identity,
            conversation=conversation,
            engagement_name=engagement.name,
            scope=scope_values,
        )

        observing = True

        async def _observing_handler(event: EventEnvelope) -> None:
            if not observing:
                # The port has no unsubscribe. Closed callbacks never steer a new run.
                if session_channel is not None and (
                    "Failed" in event.event_type or event.event_type == EventType.ACTION_DENIED
                ):
                    await on_event(event)
                return
            if identity.run_id is not None and event.run_id != identity.run_id:
                return
            identity.observe(event)
            if session_channel is not None:
                session_channel.observe(event)
            await on_event(event)

        await deps.event_bus.subscribe("*", _observing_handler)
    else:
        await deps.event_bus.subscribe("*", on_event)
    echo(f"Engagement: {engagement.name}")

    if operator_console is not None:
        if session_channel is not None:
            session_channel.attach(operator_console)
        else:
            operator_console.start()
            echo(renderer.operator_welcome_text(live=operator_console.input_available))

    def input_live() -> bool:
        return (
            session_channel.input_available
            if session_channel is not None
            else operator_console is not None and operator_console.input_available
        )

    try:
        final = await run_engagement(
            engagement,
            deps=deps,
            orchestration_config=config.orchestration,
            objective=objective,
        )

        final = await wait_for_suspensions(
            final,
            deps,
            operator_console=operator_console,
            input_live=input_live,
            echo=echo,
            non_interactive=non_interactive,
            wait_for_external_approval=wait_for_external_approval,
            session_channel=session_channel,
        )
    finally:
        if session_channel is not None:
            await session_channel.detach()
        elif operator_console is not None:
            await operator_console.aclose()
        observing = False

    return final


async def wait_for_suspensions(
    final,
    deps,
    *,
    operator_console,
    input_live,
    echo,
    non_interactive=False,
    wait_for_external_approval=False,
    session_channel=None,
):
    """Shared CLI watch path for newly-started and durably resumed runs."""
    while final.termination_reason in {
        TerminationReason.APPROVAL_PENDING,
        TerminationReason.OPERATOR_PAUSED,
    }:
        if session_channel is not None:
            session_channel.suspended(final)
        if final.termination_reason == TerminationReason.OPERATOR_PAUSED:
            live = input_live()
            echo(renderer.operator_paused_text(str(final.run_id), live=live))
            if not live:
                # No in-session channel: never block waiting for a
                # 'resume' that can never arrive. The Mission stays
                # paused and resumable from a real interactive session.
                break
            final = await _wait_for_pause(deps, final, echo=echo)
            if session_channel is not None:
                session_channel.resumed()
            continue
        console = operator_console
        live = input_live()
        if non_interactive or (not live and not wait_for_external_approval):
            # Without an explicit external-approval watch, a script/CI
            # or JSON session returns its durable pending checkpoint.
            echo(
                renderer.operator_approval_pending_text(
                    str(final.pending_approval_id), str(final.run_id)
                )
            )
            break
        if live and console is not None:
            console.set_pending_approval(final.pending_approval_id)
        try:
            final = await _wait_for_approval(deps, final, echo=echo, in_session=live)
        finally:
            if live and console is not None:
                console.clear_pending_approval()
            if session_channel is not None:
                session_channel.resumed()
    return final


async def run(
    args: argparse.Namespace,
    *,
    component_profile: ComponentProfile | None = None,
    component_registry: ComponentRegistry | None = None,
) -> int:
    async with AsyncExitStack() as stack:
        return await _run(
            args,
            mcp_stack=stack,
            component_profile=component_profile,
            component_registry=component_registry,
        )


async def _run(
    args: argparse.Namespace,
    *,
    mcp_stack: AsyncExitStack,
    component_profile: ComponentProfile | None = None,
    component_registry: ComponentRegistry | None = None,
) -> int:
    try:
        config = load_app_config_for_run(args.engagement_path)
    except (OSError, ValidationError, ValueError) as exc:
        eprint(f"ERROR: {exc}")
        return EXIT_CONFIG_ERROR

    log_level = args.log_level or config.logging.level
    logging.basicConfig(level=log_level, force=True)

    try:
        # Same conditional wiring: unset means the builtin default, and
        # existing build() test doubles keep working unpatched.
        build_options: dict[str, Any] = {}
        if component_profile is not None:
            build_options["component_profile"] = component_profile
        if component_registry is not None:
            build_options["component_registry"] = component_registry
        engagement, deps = await build(
            config, max_steps=args.max_steps, mcp_stack=mcp_stack, **build_options
        )
    except MCPConfigurationError as exc:
        eprint(f"ERROR: MCP configuration invalid: {exc}")
        return EXIT_CONFIG_ERROR
    except Exception as exc:  # noqa: BLE001 - fail closed with a readable message, not a traceback
        if logging.getLogger().isEnabledFor(logging.DEBUG):
            logging.exception("Application bootstrap failed")
        eprint(f"ERROR: failed to start application: {exc}")
        return EXIT_FAILURE

    echo: Echo = (lambda _line: None) if args.json else print

    allow_operator_input = not args.non_interactive and not getattr(args, "json", False)
    if allow_operator_input and _stdin_is_interactive():
        from pathlib import Path

        from rich.console import Console

        from redteam.app.bootstrap import build_llm_client
        from redteam.app.cli.interactive_commands import InteractiveSession
        from redteam.app.cli.session_runtime import run_session

        session = InteractiveSession(
            config.engagement, config.llm, engagement_path=Path(args.engagement_path)
        )
        result = await run_session(
            Console(),
            config,
            session=session,
            conversation=ConversationAssistant(build_llm_client(config.llm)),
            objective=getattr(args, "objective", None)
            or f"Assess the declared scope of {config.engagement.name}",
            built=(engagement, deps),
            single_run=True,
        )
        return result.exit_code
    try:
        final = await execute(
            engagement,
            deps,
            config=config,
            non_interactive=args.non_interactive,
            on_event=_event_subscriber(json_mode=args.json),
            echo=echo,
            objective=getattr(args, "objective", None),
            allow_operator_input=allow_operator_input,
            wait_for_external_approval=allow_operator_input,
        )
    except Exception as exc:  # noqa: BLE001 - same fail-closed contract as bootstrap
        if logging.getLogger().isEnabledFor(logging.DEBUG):
            logging.exception("Run failed")
        eprint(f"ERROR: run failed: {exc}")
        return EXIT_FAILURE

    if not args.json:
        _print_summary(final)
    return _exit_code_for(final)


async def _wait_for_approval(
    deps: RuntimeDeps, final: RuntimeState, *, echo: Echo, in_session: bool = False
) -> RuntimeState:
    approval_id = str(final.pending_approval_id)
    echo(renderer.operator_approval_pending_text(approval_id, str(final.run_id)))
    if in_session:
        # The same-session operator console is live; approving here resolves
        # the *exact* Approval this run is already suspended on (no replan).
        echo(renderer.operator_approval_prompt_text(approval_id))
    echo(
        "승인 대기 중 · /stop 중단 요청 · /exit 세션 종료"
        if in_session
        else "Waiting for resolution (Ctrl+C stops watching; the Run stays pending)..."
    )

    resume_settings = deps.settings.model_copy(update={"thread_id": str(final.run_id)})
    resume_runner = AgentLoopRunner(deps, settings=resume_settings)
    # deps.event_bus already has the subscriber `run()` registered before
    # the first run_engagement() call -- it's the same bus for the whole
    # process, subscribing again here would print every event twice.
    current = final
    poll_seconds = _APPROVAL_POLL_SECONDS_IN_SESSION if in_session else _APPROVAL_POLL_SECONDS
    try:
        while current.termination_reason == TerminationReason.APPROVAL_PENDING:
            result = await resume_runner.reconcile_once(current, timeout_seconds=poll_seconds)
            current = result.runtime
            mission = (
                await deps.task_graph_store.get_mission(current.mission_id)
                if current.mission_id is not None
                else None
            )
            if mission is not None and mission.status.value == "cancelled":
                current = current.model_copy(
                    update={
                        "termination_reason": TerminationReason.OPERATOR_STOPPED,
                        "pending_approval_id": None,
                        "pending_action_id": None,
                    }
                )
                break
    except KeyboardInterrupt:
        echo(
            f"\nStopped watching. Run {current.run_id} is still pending approval "
            f"{current.pending_approval_id} -- resume it later with "
            f"`redteam resume {current.run_id} <engagement.yaml>`."
        )
        raise  # let main()'s top-level handler set the conventional SIGINT exit code
    finally:
        await resume_runner.aclose()
    if current.termination_reason != TerminationReason.APPROVAL_PENDING:
        current = await settle_engagement(current, deps, last_event_id=resume_runner.last_event_id)
    return current


async def _wait_for_pause(deps: RuntimeDeps, paused: RuntimeState, *, echo: Echo) -> RuntimeState:
    settings = deps.settings.model_copy(update={"thread_id": str(paused.run_id)})
    runner = AgentLoopRunner(deps, settings=settings)
    current = paused
    try:
        while current.termination_reason == TerminationReason.OPERATOR_PAUSED:
            result = await runner.reconcile_once(
                current, timeout_seconds=_APPROVAL_POLL_SECONDS_IN_SESSION
            )
            current = result.runtime
            mission = (
                await deps.task_graph_store.get_mission(current.mission_id)
                if current.mission_id is not None
                else None
            )
            if mission is not None and mission.status.value == "cancelled":
                current = current.model_copy(
                    update={"termination_reason": TerminationReason.OPERATOR_STOPPED}
                )
                break
    finally:
        await runner.aclose()
    if current.termination_reason not in {
        TerminationReason.OPERATOR_PAUSED,
        TerminationReason.APPROVAL_PENDING,
    }:
        return await settle_engagement(current, deps, last_event_id=runner.last_event_id)
    return current


def _print_summary(final: RuntimeState) -> None:
    suspended = final.termination_reason in {
        TerminationReason.OPERATOR_PAUSED,
        TerminationReason.APPROVAL_PENDING,
    }
    print(
        f"\nRun {'suspended' if suspended else 'finished'}: phase={final.phase.value} "
        f"termination_reason={final.termination_reason}"
    )
    print(f"RunId: {final.run_id}")
    if final.mission_status is not None:
        print(f"Mission: {final.mission_status.value}")
    print(f"Actions executed: {final.actions_executed}")
    if final.termination_reason not in {
        TerminationReason.OPERATOR_PAUSED,
        TerminationReason.APPROVAL_PENDING,
    }:
        print(f"Report: redteam report generate {final.run_id}")


def _exit_code_for(final: RuntimeState) -> int:
    # A policy-correct DENY/APPROVAL_PENDING/budget-exhausted stop is not
    # a CLI failure -- only a run that actually crashed is (see section 20).
    if final.phase.value == "failed":
        return EXIT_FAILURE
    return EXIT_OK
