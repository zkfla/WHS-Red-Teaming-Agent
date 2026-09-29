"""Stage 6 durable-resume safety regressions (Prompt 6).

Auditing the durable-resume / pending-approval path found that
``AgentLoopRunner._resume_pending_approval`` refused to resume an
``APPROVAL_PENDING`` run only when its Mission was ``PAUSED``. An operator
STOP sets the Mission to ``CANCELLED`` (see ``OperatorIntentService._stop``),
and COMPLETED/FAILED/BLOCKED are equally deliberate settled outcomes -- a
later approval of the held action must not override any of them.

An approval only admits the exact held ``ActionProposal``; it is not an
operator instruction. So resume must require the Mission be ``ACTIVE`` (or
absent, for legacy runs with no Mission), exactly like the
``OPERATOR_PAUSED`` resume path. Anything else fails closed *before* the
Control Plane's ``approval_outcome`` is consulted, so a refused resume can
never consume the resolution -- and never re-enters the graph.

Focused runner-level tests with fakes only -- no transport E2E.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from redteam.core.contracts.action import ActionProposal
from redteam.core.contracts.mission import MissionPlan, Provenance
from redteam.core.contracts.policy import PolicyDecision
from redteam.core.contracts.target import HostTarget
from redteam.core.enums import AgentPhase, MissionStatus, PolicyVerdict
from redteam.core.ids import (
    ActionId,
    AgentId,
    ApprovalId,
    EngagementId,
    MissionId,
    RunId,
)
from redteam.orchestration.runner import AgentLoopRunner
from redteam.orchestration.state import RuntimeState, TerminationReason
from redteam.orchestration.task_graph.memory import InMemoryTaskGraphStore
from tests.fakes import FakeControlPlane, build_test_application

RUN_ID = RunId("run_stage6")
ENGAGEMENT_ID = EngagementId("eng_stage6")
MISSION_ID = MissionId("mission_stage6")
ACTION_ID = ActionId("action_held")
APPROVAL_ID = ApprovalId("appr_held")
NOW = datetime.now(timezone.utc)


class _RecordingPlane(FakeControlPlane):
    """Fake Control Plane that counts ``approval_outcome`` consultations."""

    def __init__(self, decision: PolicyDecision | None = None) -> None:
        super().__init__()
        self.approval_calls = 0
        self._decision = decision

    async def approval_outcome(self, approval_id: ApprovalId) -> PolicyDecision | None:
        self.approval_calls += 1
        return self._decision


class _FakeGraph:
    """Only enough graph to serve the latest checkpoint; any attempt to
    actually re-enter (``ainvoke``) raises, so a test can prove refusal."""

    def __init__(self, snapshot: object) -> None:
        self._snapshot = snapshot

    async def aget_state(self, config: object) -> object:  # noqa: ARG002
        return self._snapshot


def _proposal() -> ActionProposal:
    return ActionProposal(
        action_id=ACTION_ID,
        run_id=RUN_ID,
        engagement_id=ENGAGEMENT_ID,
        agent_id=AgentId("mobile-agent"),
        capability="mobile",
        operation="static_analysis",
        target=HostTarget(host="a.local"),
        reason="held for approval",
        created_at=NOW,
    )


async def _runner_with_mission(
    status: MissionStatus | None, plane: FakeControlPlane
) -> tuple[AgentLoopRunner, RuntimeState]:
    """A runner whose latest safe checkpoint holds the exact pending
    proposal, with the Mission (if any) in ``status``."""
    store = InMemoryTaskGraphStore()
    mission_id: MissionId | None = None
    if status is not None:
        mission_id = MISSION_ID
        await store.create_mission(
            MissionPlan(
                mission_id=MISSION_ID,
                engagement_id=ENGAGEMENT_ID,
                objective="hold the line",
                status=status,
                provenance=Provenance(source="operator"),
                created_at=NOW,
                updated_at=NOW,
            )
        )
    deps = replace(build_test_application(control_plane=plane), task_graph_store=store)
    runner = AgentLoopRunner(deps, settings=deps.settings)
    runtime = RuntimeState(
        run_id=RUN_ID,
        engagement_id=ENGAGEMENT_ID,
        mission_id=mission_id,
        phase=AgentPhase.FINISHED,
        termination_reason=TerminationReason.APPROVAL_PENDING,
        pending_action_id=ACTION_ID,
        pending_approval_id=APPROVAL_ID,
    )
    snapshot = SimpleNamespace(
        values={"runtime": runtime, "action_proposal": _proposal(), "plan_step": None},
        next=[],
    )
    runner._graph = _FakeGraph(snapshot)  # noqa: SLF001 - focused safety check
    return runner, runtime


@pytest.mark.parametrize(
    "status",
    [
        MissionStatus.PAUSED,
        MissionStatus.CANCELLED,
        MissionStatus.COMPLETED,
        MissionStatus.FAILED,
        MissionStatus.BLOCKED,
        MissionStatus.DRAFT,
    ],
)
async def test_approval_resume_fails_closed_unless_mission_is_active(
    status: MissionStatus,
) -> None:
    """A resolved approval must never override an operator stop/pause or a
    settled Mission: the run is left exactly as terminated and the resolution
    is not even consulted."""
    resolved = PolicyDecision(
        action_id=ACTION_ID,
        verdict=PolicyVerdict.ALLOW,
        reasons=["approved"],
        matched_rules=[],
        approval_id=APPROVAL_ID,
        evaluated_at=NOW,
    )
    plane = _RecordingPlane(resolved)
    runner, runtime = await _runner_with_mission(status, plane)

    result = await runner.resume_recovered(runtime)

    assert result is runtime  # left exactly as terminated
    assert plane.approval_calls == 0  # resolution never consumed


async def test_approval_resume_consults_the_resolution_once_mission_is_active() -> None:
    """Positive control: an ACTIVE Mission passes the operator-state gate and
    reaches Control for the resolved outcome."""
    plane = _RecordingPlane(None)  # still pending -> gate passes, no re-enter
    runner, runtime = await _runner_with_mission(MissionStatus.ACTIVE, plane)

    result = await runner.resume_recovered(runtime)

    assert plane.approval_calls == 1
    assert result is runtime
    assert result.termination_reason == TerminationReason.APPROVAL_PENDING


async def test_approval_resume_without_a_mission_still_consults_the_resolution() -> None:
    """Legacy runs with no Mission keep the pre-existing behavior: the
    operator-state gate is inert."""
    plane = _RecordingPlane(None)
    runner, runtime = await _runner_with_mission(None, plane)

    result = await runner.resume_recovered(runtime)

    assert plane.approval_calls == 1
    assert result is runtime


@pytest.mark.parametrize("mismatch", ["run_id", "engagement_id"])
async def test_approval_resume_rejects_cross_run_or_engagement_proposal(mismatch: str) -> None:
    plane = _RecordingPlane(None)
    runner, runtime = await _runner_with_mission(MissionStatus.ACTIVE, plane)
    snapshot = runner._graph._snapshot  # noqa: SLF001 - checkpoint forgery regression
    snapshot.values["action_proposal"] = _proposal().model_copy(
        update={mismatch: (
            RunId("run_unrelated") if mismatch == "run_id" else EngagementId("eng_unrelated")
        )}
    )

    assert await runner.resume_recovered(runtime) is runtime
    assert plane.approval_calls == 0
