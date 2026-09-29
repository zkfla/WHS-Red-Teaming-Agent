"""Persistent operator console (PROMPT 4+5) -- the runtime input channel
``commands.run.execute`` attaches in interactive sessions.

Focused on the two things a tool/policy integration could get wrong:

- ``query``/``status`` and ``focus``/``reprioritize`` only touch the Mission
  and Task Graph through ``OperatorIntentService`` -- no tool gateway and no
  Control Plane policy path is ever reached.
- in-session ``approve``/``deny`` resolves the *exact* Approval this run is
  suspended on (never a different id), through the control plane's own
  ``resolve_approval`` -- no replan, no second Approval.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
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
from redteam.core.contracts.mission import MissionPlan, MissionTask, Provenance
from redteam.core.enums import (
    AgentPhase,
    ApprovalStatus,
    MissionStatus,
    OperatorIntentType,
    TaskStatus,
)
from redteam.core.ids import AgentId, EngagementId, MissionId, RunId, TaskId
from redteam.event_bus.memory import InMemoryEventBus
from redteam.orchestration.operator_intents import (
    InMemoryOperatorIntentStore,
    IntentStatus,
    OperatorIntentService,
)
from redteam.orchestration.state import RuntimeState
from redteam.orchestration.task_graph.memory import InMemoryTaskGraphStore

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
RUN = RunId("run_operator")
ENG = EngagementId("eng_operator")
MISSION = MissionId("mission_operator")


class RecordingToolGateway:
    """A tool gateway that records every call. The operator channel must
    never reach it -- it has no execution path by construction."""

    def __init__(self) -> None:
        self.calls: list[object] = []

    async def execute(self, invocation: object) -> object:  # pragma: no cover - must stay empty
        self.calls.append(invocation)
        raise AssertionError("operator channel must not invoke a tool")


class RecordingControlPlane:
    """Only the two methods the console uses; records resolutions so the
    test can assert the exact Approval identity."""

    def __init__(self) -> None:
        self.resolved: list[tuple[str, ApprovalStatus, str]] = []
        self.outcomes: dict[str, object] = {}

    async def resolve_approval(self, approval_id: object, status: ApprovalStatus, resolved_by: str):
        self.resolved.append((str(approval_id), status, resolved_by))
        return SimpleNamespace(approval_id=str(approval_id), status=status)

    async def approval_outcome(self, approval_id: object):  # pragma: no cover - not used here
        return self.outcomes.get(str(approval_id))


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


def _task(*, priority: int = 10, capability: str = "web", status: TaskStatus = TaskStatus.READY):
    return MissionTask(
        task_id=TaskId("task_web"),
        mission_id=MISSION,
        title="task_web",
        objective="Investigate web surface",
        status=status,
        priority=priority,
        required_capability=capability,
        assigned_agent=AgentId("web-api-agent"),
        provenance=Provenance(source="engagement:bootstrap"),
        created_at=NOW,
        updated_at=NOW,
    )


async def _console(
    *,
    with_mission: bool = True,
    task: MissionTask | None = None,
    mission_status: MissionStatus = MissionStatus.ACTIVE,
):
    task_store = InMemoryTaskGraphStore()
    if with_mission:
        await task_store.create_mission(_mission(mission_status))
        if task is not None:
            await task_store.create_task(task)
    intent_store = InMemoryOperatorIntentStore()
    gateway = RecordingToolGateway()
    control_plane = RecordingControlPlane()
    deps = SimpleNamespace(
        operator_intent_store=intent_store,
        task_graph_store=task_store,
        event_bus=InMemoryEventBus(),
        control_plane=control_plane,
        tool_gateway=gateway,
    )
    lines: list[str] = []
    identity = run_command.RuntimeIdentity(run_id=RUN, engagement_id=ENG, mission_id=MISSION)
    console = run_command.OperatorConsole(
        deps, lines.append, identity=identity, resolved_by="tester"
    )
    return console, lines, gateway, intent_store, task_store, control_plane


# --- no tool / no policy on query and priority ---------------------------


async def test_status_query_applies_intent_without_touching_a_tool() -> None:
    console, lines, gateway, intent_store, _tasks, control_plane = await _console()

    await console.handle_line("status")

    assert gateway.calls == []
    assert control_plane.resolved == []
    records = await intent_store.list_for_mission(MISSION)
    assert records[-1].status is IntentStatus.APPLIED
    assert records[-1].intent.intent_type is OperatorIntentType.STATUS_QUERY
    assert any("mission" in line for line in lines)


async def test_query_alias_is_interpreted_as_a_status_query() -> None:
    console, _lines, gateway, intent_store, _tasks, _cp = await _console()

    await console.handle_line("query")

    records = await intent_store.list_for_mission(MISSION)
    assert records[-1].intent.intent_type is OperatorIntentType.STATUS_QUERY
    assert gateway.calls == []


async def test_focus_and_reprioritize_change_priority_without_a_tool() -> None:
    console, _lines, gateway, _intent_store, task_store, control_plane = await _console(
        task=_task(priority=10)
    )

    await console.handle_line("focus web")
    focused = (await task_store.list_tasks(mission_id=MISSION))[0]
    assert focused.priority == 500  # OperatorIntentService.FOCUS_PRIORITY
    assert gateway.calls == []

    await console.handle_line("reprioritize web 90")
    reprioritized = (await task_store.list_tasks(mission_id=MISSION))[0]
    assert reprioritized.priority == 90
    assert gateway.calls == []
    assert control_plane.resolved == []


async def test_explain_renders_explanation_without_a_tool() -> None:
    console, lines, gateway, intent_store, _tasks, _cp = await _console()

    await console.handle_line("explain")

    records = await intent_store.list_for_mission(MISSION)
    assert records[-1].intent.intent_type is OperatorIntentType.EXPLANATION_QUERY
    assert any(line.startswith("operator>") for line in lines)
    assert gateway.calls == []


@pytest.mark.parametrize(
    ("line", "mission_status", "expected"),
    [
        ("pause", MissionStatus.ACTIVE, OperatorIntentType.PAUSE),
        ("resume", MissionStatus.PAUSED, OperatorIntentType.RESUME),
        ("stop", MissionStatus.ACTIVE, OperatorIntentType.STOP),
        ("exclude mobile", MissionStatus.ACTIVE, OperatorIntentType.EXCLUDE),
    ],
)
async def test_control_commands_route_to_expected_intents_without_a_tool(
    line: str, mission_status: MissionStatus, expected: OperatorIntentType
) -> None:
    console, _lines, gateway, intent_store, _tasks, control_plane = await _console(
        mission_status=mission_status
    )

    await console.handle_line(line)

    records = await intent_store.list_for_mission(MISSION)
    assert records[-1].status is IntentStatus.APPLIED
    assert records[-1].intent.intent_type is expected
    assert gateway.calls == []
    assert control_plane.resolved == []


async def test_rejected_instruction_is_reported_without_a_tool() -> None:
    console, lines, gateway, _intent_store, _tasks, _cp = await _console()

    await console.handle_line("do the thing")

    assert gateway.calls == []
    assert any("rejected" in line for line in lines)


# --- in-session approval identity ---------------------------------------


async def test_approve_resolves_the_exact_pending_approval() -> None:
    console, _lines, _gateway, _intent_store, _tasks, control_plane = await _console()
    console.set_pending_approval("appr_exact")

    await console.handle_line("approve")

    assert control_plane.resolved == [("appr_exact", ApprovalStatus.APPROVED, "tester")]
    # A second approve must not mint/resolve anything else.
    await console.handle_line("approve")
    assert control_plane.resolved == [("appr_exact", ApprovalStatus.APPROVED, "tester")]


async def test_deny_rejects_the_exact_pending_approval() -> None:
    console, _lines, _gateway, _intent_store, _tasks, control_plane = await _console()
    console.set_pending_approval("appr_exact")

    await console.handle_line("deny")

    assert control_plane.resolved == [("appr_exact", ApprovalStatus.REJECTED, "tester")]


async def test_mismatched_explicit_id_is_refused_and_nothing_is_resolved() -> None:
    console, lines, _gateway, _intent_store, _tasks, control_plane = await _console()
    console.set_pending_approval("appr_exact")

    await console.handle_line("deny appr_other")

    assert control_plane.resolved == []
    assert any("does not match" in line for line in lines)
    # Still suspended on the original id.
    await console.handle_line("approve")
    assert control_plane.resolved == [("appr_exact", ApprovalStatus.APPROVED, "tester")]


async def test_approve_with_no_pending_approval_is_a_no_op() -> None:
    console, lines, _gateway, _intent_store, _tasks, control_plane = await _console()

    await console.handle_line("approve")

    assert control_plane.resolved == []
    assert any("no action is awaiting approval" in line for line in lines)


async def test_explicit_matching_id_is_accepted() -> None:
    console, _lines, _gateway, _intent_store, _tasks, control_plane = await _console()
    console.set_pending_approval("appr_exact")

    await console.handle_line("approve appr_exact")

    assert control_plane.resolved == [("appr_exact", ApprovalStatus.APPROVED, "tester")]


# --- misc ---------------------------------------------------------------


def test_operator_texts_survive_rich_markup_rendering() -> None:
    """The interactive path prints these through ``rich.Console``; a square
    bracket in the text would be swallowed as a markup tag, so assert the
    command tokens actually reach the rendered output."""

    import io

    from rich.console import Console

    from redteam.app.cli import renderer

    buffer = io.StringIO()
    console = Console(file=buffer, force_terminal=False, width=200)
    console.print(renderer.operator_help_text())
    output = buffer.getvalue()
    for token in ("approve ID", "deny ID", "pause", "resume", "stop", "focus", "exclude", "status"):
        assert token in output


async def test_help_lists_in_session_controls() -> None:
    console, lines, _gateway, _intent_store, _tasks, _cp = await _console()

    await console.handle_line("help")

    help_text = "\n".join(lines)
    for command in ("pause", "resume", "stop", "focus", "exclude", "status", "approve", "deny"):
        assert command in help_text


async def test_command_before_mission_is_known_is_a_notice_not_a_crash() -> None:
    console, _lines, _gateway, _intent_store, _tasks, _cp = await _console()
    console._identity = run_command.RuntimeIdentity(run_id=RUN, engagement_id=ENG, mission_id=None)

    # Should not raise.
    await console.handle_line("pause")


async def test_intent_channel_unavailable_when_not_wired() -> None:
    deps = SimpleNamespace(
        event_bus=InMemoryEventBus(), control_plane=RecordingControlPlane()
    )
    lines: list[str] = []
    console = run_command.OperatorConsole(
        deps,
        lines.append,
        identity=run_command.RuntimeIdentity(run_id=RUN, engagement_id=ENG, mission_id=MISSION),
    )

    await console.handle_line("pause")

    assert any("not wired" in line for line in lines)


# --- CLI wiring: input stays off for --non-interactive / --json ----------


def _engagement_yaml(tmp_path: Path) -> Path:
    path = tmp_path / "engagement.yaml"
    path.write_text(
        """
name: "Operator Wiring"
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


async def _run_with_flags(tmp_path: Path, monkeypatch, *, non_interactive: bool, json: bool):
    # build/execute are mocked below, but the real load_app_config_for_run()
    # still runs first and now requires an explicit provider (section 4/20)
    # -- there is no --config app.yaml path any more.
    monkeypatch.setenv("REDTEAM_LLM_PROVIDER", "fake")
    config = SimpleNamespace(engagement_path=str(_engagement_yaml(tmp_path)))
    engagement = Engagement(
        engagement_id=EngagementId("eng_wiring"),
        name="Operator Wiring",
        scope=EngagementScope(allowed_hosts=["a.local"]),
        execution_budget=ExecutionBudget(max_actions=1),
        rate_limit_policy=RateLimitPolicy(max_actions_per_minute=10),
        approval_policy=ApprovalPolicy(),
        created_at=datetime.now(timezone.utc),
    )
    deps = SimpleNamespace()
    build_mock = AsyncMock(return_value=(engagement, deps))
    execute_mock = AsyncMock(return_value=_finished_runtime())
    monkeypatch.setattr(run_command, "build", build_mock)
    monkeypatch.setattr(run_command, "execute", execute_mock)

    args = argparse.Namespace(
        engagement_path=config.engagement_path,
        non_interactive=non_interactive,
        max_steps=None,
        log_level=None,
        json=json,
    )
    await run_command.run(args)
    return execute_mock.await_args.kwargs["allow_operator_input"]


@pytest.mark.parametrize(
    ("non_interactive", "json", "expected"),
    [
        (True, False, False),
        (False, True, False),
        (False, False, True),
    ],
)
async def test_operator_input_only_enabled_for_an_interactive_plain_run(
    tmp_path: Path, monkeypatch, non_interactive: bool, json: bool, expected: bool
) -> None:
    enabled = await _run_with_flags(
        tmp_path, monkeypatch, non_interactive=non_interactive, json=json
    )
    assert enabled is expected


def test_operator_console_imports_without_service(monkeypatch) -> None:
    # Guard against a hard import of OperatorIntentService at class-def time
    # regressing: from_deps must tolerate a deps without the store.
    service = OperatorIntentService.from_deps(SimpleNamespace())
    assert service is None
