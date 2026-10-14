"""Offline terminal diagnostic persistence, stale-state checks and report rendering."""

from redteam.app.central_views import inspect_central_run
from redteam.app.cli.commands.report import _render_markdown
from redteam.core.enums import AgentPhase, MissionStatus
from redteam.orchestration.diagnostics import DecisionDiagnostic
from redteam.orchestration.state import TerminationReason
from tests.integration.test_central_completion import selection


async def test_planning_failure_survives_readonly_report_reopen(tmp_path):
    diagnostic = DecisionDiagnostic(
        code="decision_repair_budget_exhausted", stage="hypothesis_planning", kind="budget",
        attempts=4, summary="Repair attempts exhausted.", retryable=False,
        last_cause=DecisionDiagnostic(
            code="model_validation", stage="hypothesis_planning", kind="format",
            summary="Include the required field.", location="questions[0].objective",
            validation_issues=[{"rule": "missing", "location": "questions[0].objective"}],
        ),
    )
    async with selection(tmp_path) as (session, _):
        final = await session.settle(session.runtime.model_copy(update={
            "phase": AgentPhase.FAILED, "termination_reason": TerminationReason.PLANNING_ERROR,
            "planning_diagnostic": diagnostic, "planning_diagnostic_history": [diagnostic],
        }))
        path, run_id = session.deps.central_state.path, str(final.run_id)
    _, _, report = await inspect_central_run(path, run_id)
    assert report.phase is AgentPhase.FAILED
    assert report.planning_diagnostic == diagnostic
    assert report.planning_diagnostic_history == [diagnostic]
    rendered = _render_markdown(report)
    assert "decision_repair_budget_exhausted" in rendered
    assert "questions[0].objective" in rendered


async def test_diagnostic_snapshot_rejects_changed_mission_and_clears_on_new_settle(tmp_path):
    diagnostic = DecisionDiagnostic(code="model_validation", stage="hypothesis_planning",
                                    kind="format", summary="Include the required field.")
    async with selection(tmp_path) as (session, _):
        await session.settle(session.runtime.model_copy(update={
            "termination_reason": TerminationReason.PLANNING_ERROR,
            "planning_diagnostic": diagnostic,
        }))
        state = session.deps.central_state
        def revise(tx):
            mission = tx.get_mission(tx.binding()[0])
            tx.update_mission(mission.model_copy(update={"revision": mission.revision + 1}),
                              expected_revision=mission.revision)
        await state.transaction(revise)
        assert await state.transaction(lambda tx: tx.planning_diagnostics(), write=False) == (
            None, [],
        )
        await session.settle(session.runtime.model_copy(update={
            "termination_reason": TerminationReason.OPERATOR_PAUSED,
            "planning_diagnostic": None,
        }))
        _, _, report = await inspect_central_run(state.path, state.run_id)
        assert report.planning_diagnostic is None
        assert report.planning_diagnostic_history == []


async def test_settle_cancelled_mission_without_terminal_reason_remains_valid(tmp_path):
    async with selection(tmp_path) as (session, _):
        def cancel(tx):
            mission = tx.get_mission(tx.binding()[0])
            tx.update_mission(mission.model_copy(update={
                "status": MissionStatus.CANCELLED, "revision": mission.revision + 1,
            }), expected_revision=mission.revision)
        await session.deps.central_state.transaction(cancel)
        final = await session.settle(session.runtime)
        assert final.termination_reason is TerminationReason.OPERATOR_STOPPED
        assert await session.deps.central_state.transaction(
            lambda tx: tx.planning_diagnostics(), write=False,
        ) == (None, [])


async def test_stop_after_planning_error_does_not_publish_stale_diagnostic(tmp_path):
    async with selection(tmp_path) as (session, _):
        await session.settle(session.runtime.model_copy(update={
            "termination_reason": TerminationReason.PLANNING_ERROR,
            "planning_diagnostic": DecisionDiagnostic(
                code="hypothesis_proposal_invalid", stage="hypothesis_planning",
                kind="validation", summary="Invalid proposal.",
            ),
        }))
        def cancel(tx):
            mission = tx.get_mission(tx.binding()[0])
            tx.update_mission(mission.model_copy(update={
                "status": MissionStatus.CANCELLED, "revision": mission.revision + 1,
            }), expected_revision=mission.revision)
        await session.deps.central_state.transaction(cancel)
        final = await session.settle(session.runtime)
        assert final.termination_reason is TerminationReason.OPERATOR_STOPPED
        assert await session.deps.central_state.transaction(
            lambda tx: tx.planning_diagnostics(), write=False,
        ) == (None, [])
