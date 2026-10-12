"""Operator-console hardening: non-TTY fail-safe, reader stability, concise
approval notices, same-session pause/resume, and full natural-language routes.

These complement ``test_cli_operator_console.py`` (which covers the tool/policy
boundary and approval identity) by pinning the failure modes a non-interactive
or half-broken input channel would otherwise hide:

- stdin is not a TTY -> never claim live input, never block at APPROVAL_PENDING;
- the prompt reader raising must be *visible* and must disable input;
- pause must resume through the *same* session's console, not require a restart;
- operator notices stay one line and ASCII-safe for the plain renderer.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import cast

import pytest

from redteam.app.cli import renderer
from redteam.app.cli.commands import run as run_command
from redteam.core.contracts.mission import MissionPlan, MissionTask, Provenance
from redteam.core.enums import (
    AgentPhase,
    ApprovalStatus,
    MissionStatus,
    OperatorIntentType,
    TaskStatus,
)
from redteam.core.ids import AgentId, ApprovalId, EngagementId, MissionId, RunId, TaskId
from redteam.event_bus.memory import InMemoryEventBus
from redteam.orchestration.context import RuntimeDeps
from redteam.orchestration.coordination import coordinator_for
from redteam.orchestration.operator_intents import (
    InMemoryOperatorIntentStore,
    IntentStatus,
)
from redteam.orchestration.state import RuntimeState, TerminationReason
from redteam.orchestration.task_graph.memory import InMemoryTaskGraphStore


@pytest.fixture(autouse=True)
def explicit_legacy_runtime(monkeypatch):
    """These fixtures exercise the retained legacy CLI/schema, explicitly selected."""
    monkeypatch.setenv("REDTEAM_RUNTIME_BACKEND", "legacy")

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
RUN = RunId("run_hardening")
ENG = EngagementId("eng_hardening")
MISSION = MissionId("mission_hardening")


def _mission(status: MissionStatus = MissionStatus.ACTIVE) -> MissionPlan:
    return MissionPlan(
        mission_id=MISSION,
        engagement_id=ENG,
        objective="Assess the declared scope",
        success_criteria=["evidence-backed result"],
        status=status,
        created_at=NOW,
        updated_at=NOW,
        provenance=Provenance(source="test"),
    )


def _task() -> MissionTask:
    return MissionTask(
        task_id=TaskId("task_web"),
        mission_id=MISSION,
        title="task_web",
        objective="Investigate web surface",
        status=TaskStatus.READY,
        priority=10,
        required_capability="web",
        assigned_agent=AgentId("web-api-agent"),
        provenance=Provenance(source="engagement:bootstrap"),
        created_at=NOW,
        updated_at=NOW,
    )


def _runtime(
    reason: str,
    *,
    pending_approval_id: ApprovalId | None = None,
    mission_id: MissionId | None = MISSION,
) -> RuntimeState:
    return RuntimeState(
        run_id=RUN,
        engagement_id=ENG,
        phase=AgentPhase.FINISHED,
        termination_reason=reason,
        pending_approval_id=pending_approval_id,
        mission_id=mission_id,
    )


async def _deps(*, with_mission: bool = True) -> SimpleNamespace:
    task_store = InMemoryTaskGraphStore()
    if with_mission:
        await task_store.create_mission(_mission())
        await task_store.create_task(_task())
        await coordinator_for(task_store).bind(str(RUN), str(MISSION), str(ENG))
    return SimpleNamespace(
        operator_intent_store=InMemoryOperatorIntentStore(),
        task_graph_store=task_store,
        investigations=task_store,
        central_state=None,
        event_bus=InMemoryEventBus(),
        control_plane=SimpleNamespace(),
        tool_gateway=SimpleNamespace(),
        settings=SimpleNamespace(),
    )


# --- non-TTY: no false "live input", no approval hang ----------------------


async def test_non_tty_start_does_not_claim_live_input() -> None:
    deps = await _deps()
    lines: list[str] = []
    console = run_command.OperatorConsole(
        cast(RuntimeDeps, deps), lines.append, identity=run_command.RuntimeIdentity()
    )

    assert console.input_available is False  # answerable before start()

    console.start()

    assert console.input_available is False
    assert console._reader_task is None
    await console.aclose()






def test_pending_summary_is_suspended_not_finished(capsys: pytest.CaptureFixture[str]) -> None:
    run_command._print_summary(
        _runtime(
            TerminationReason.APPROVAL_PENDING,
            pending_approval_id=ApprovalId("appr_suspended"),
        )
    )
    out = capsys.readouterr().out
    assert "Run suspended:" in out
    assert "Run finished:" not in out












# --- reader exceptions: visible and fail-safe ------------------------------


class _RaisingSession:
    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    async def prompt_async(self, _prompt: str = "") -> str:
        raise self._exc


def _reader_console(lines: list[str]) -> run_command.OperatorConsole:
    deps = SimpleNamespace(event_bus=InMemoryEventBus())
    console = run_command.OperatorConsole(
        cast(RuntimeDeps, deps), lines.append, identity=run_command.RuntimeIdentity()
    )
    console._queue = asyncio.Queue()
    console._input_live = True
    return console


async def test_reader_exception_is_surfaced_and_disables_input() -> None:
    lines: list[str] = []
    console = _reader_console(lines)
    console._prompt_session = _RaisingSession(RuntimeError("tty went away"))  # type: ignore[assignment]

    await console._read_prompt()

    assert any("operator input stopped" in line for line in lines)
    assert console.input_available is False


async def test_reader_exception_never_prints_a_raw_token() -> None:
    lines: list[str] = []
    console = _reader_console(lines)
    console._prompt_session = _RaisingSession(  # type: ignore[assignment]
        RuntimeError("prompt failed token=private-value")
    )

    await console._read_prompt()

    assert "private-value" not in "\n".join(lines)
    assert "REDACTED" in "\n".join(lines)


async def test_reader_eof_is_quiet_and_disables_input() -> None:
    lines: list[str] = []
    console = _reader_console(lines)
    console._prompt_session = _RaisingSession(EOFError())  # type: ignore[assignment]

    await console._read_prompt()

    assert lines == []
    assert console.input_available is False


# --- same-session pause/resume ---------------------------------------------






# --- concise, honest notices ----------------------------------------------


def test_welcome_text_is_honest_about_live_input() -> None:
    live = renderer.operator_welcome_text(live=True)
    offline = renderer.operator_welcome_text(live=False)

    assert "live input enabled" in live
    assert "no interactive input" in offline
    assert "live input" not in offline


def test_approval_and_pause_notices_are_single_line_and_actionable() -> None:
    approval = renderer.operator_approval_prompt_text("appr_9")
    pending = renderer.operator_approval_pending_text("appr_9", "run_9")
    paused_live = renderer.operator_paused_text("run_9", live=True)
    paused_offline = renderer.operator_paused_text("run_9", live=False)

    assert "\n" not in approval
    assert "\n" not in pending
    assert "approve" in approval and "deny" in approval
    assert "redteam approvals approve|deny appr_9" in pending
    assert "redteam resume run_9" in pending
    assert "resume" in paused_live
    assert "no interactive input" in paused_offline


# --- full natural-language routes -----------------------------------------


class _RecordingControlPlane:
    def __init__(self) -> None:
        self.resolved: list[tuple[str, ApprovalStatus, str]] = []

    async def resolve_approval(self, approval_id: object, status: ApprovalStatus, resolved_by: str):
        self.resolved.append((str(approval_id), status, resolved_by))
        return SimpleNamespace(approval_id=str(approval_id), status=status)


async def _nl_console() -> tuple[run_command.OperatorConsole, list[str]]:
    deps = await _deps()
    deps.control_plane = _RecordingControlPlane()
    lines: list[str] = []
    identity = run_command.RuntimeIdentity(run_id=RUN, engagement_id=ENG, mission_id=MISSION)
    return (
        run_command.OperatorConsole(
            cast(RuntimeDeps, deps), lines.append, identity=identity, resolved_by="tester"
        ),
        lines,
    )


async def test_punctuation_does_not_defeat_console_aliases() -> None:
    console, _lines = await _nl_console()

    await console.handle_line("status?")
    await console.handle_line("pause!")

    records = await console._deps.operator_intent_store.list_for_mission(MISSION)  # type: ignore[union-attr]
    types = [record.intent.intent_type for record in records]
    assert OperatorIntentType.STATUS_QUERY in types
    assert OperatorIntentType.PAUSE in types


async def test_lone_question_mark_still_shows_help() -> None:
    console, lines = await _nl_console()

    await console.handle_line("?")

    assert any("operator console commands" in line for line in lines)



@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("please pause for a moment", OperatorIntentType.PAUSE),
        ("stop the mission now", OperatorIntentType.STOP),
        ("focus on the web surface", OperatorIntentType.FOCUS),
        ("what is the current status?", OperatorIntentType.STATUS_QUERY),
        ("why did you stop?", OperatorIntentType.EXPLANATION_QUERY),
        ("잠시 멈춰줘", OperatorIntentType.PAUSE),
        ("현재 상태 알려줘", OperatorIntentType.STATUS_QUERY),
    ],
)
async def test_full_sentences_route_to_the_intended_intent(
    line: str, expected: OperatorIntentType
) -> None:
    console, _lines = await _nl_console()

    await console.handle_line(line)

    records = await console._deps.operator_intent_store.list_for_mission(MISSION)  # type: ignore[union-attr]
    assert records[-1].status is IntentStatus.APPLIED
    assert records[-1].intent.intent_type is expected


async def test_bar_approval_shortcut_still_resolves_exact_approval() -> None:
    console, _lines = await _nl_console()
    control_plane = cast(_RecordingControlPlane, console._deps.control_plane)
    console.set_pending_approval("appr_exact")

    await console.handle_line("approve!")

    assert control_plane.resolved == [("appr_exact", ApprovalStatus.APPROVED, "tester")]
