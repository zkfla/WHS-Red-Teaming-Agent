"""Settlement maps only a current durable replan exhaustion to no actionable work."""

from contextlib import asynccontextmanager

import pytest

from redteam.app.central_run import open_central_run
from redteam.app.central_views import inspect_central_run
from redteam.core.config import AppConfig, EngagementConfig
from redteam.core.enums import MissionStatus
from redteam.llm.fake import FakeLLMClient
from redteam.orchestration.central_completion import GOAL_ORIGIN_CHANGED_CODE
from redteam.orchestration.central_results import RESULT_UPDATED
from redteam.orchestration.central_runtime import event
from redteam.orchestration.specialist_planner import SpecialistPlanner
from redteam.orchestration.state import TerminationReason
from tests.integration.test_central_completion import OriginalProtocolDouble, finish_inconclusive
from tests.integration.test_central_development import engagement, gateway
from tests.integration.test_central_parallel import limits


@asynccontextmanager
async def selected_run(tmp_path):
    original = engagement()
    original.metadata["success_criteria"] = ["Assess the original response"]
    config = AppConfig(engagement=EngagementConfig.model_validate(
        original.model_dump(exclude={"engagement_id", "created_at"}),
    ))
    configured = limits()[1]
    llm = FakeLLMClient()
    tools = OriginalProtocolDouble(gateway())
    directory = tmp_path / "run"
    async with open_central_run(
        config,
        directory=directory,
        settings=configured,
        llm=llm,
        tool_gateway=tools,
        tool_catalog=tools.catalog,
    ) as session:
        yield session, llm, tools, config, configured, directory


async def exhaust_replan_budget(session):
    state = session.deps.central_state
    kinds = tuple(SpecialistPlanner(session.deps).wake_events())

    def commit(tx, index):
        sequence = tx.latest_event_sequence(kinds)
        tx.finish_hypothesis_judgment(f"cap-exhausted-{index}", {
            "termination": "replan",
            "reason": "No task can be proposed from the current originals",
            "resume_conditions": [],
            "sequence": sequence,
            "replan_max_passes": 3,
            "tasks": [],
            "questions": [],
        })

    for index in range(4):
        await state.transaction(lambda tx, index=index: commit(tx, index))
    status = await state.planner_progress_status()
    assert status is not None and status.kind == "replan_exhausted"
    return status


async def make_completion_review_fail(session, llm):
    mission = await session.deps.central_state.get_mission(session.runtime.mission_id)
    llm._canned_structured = {
        "accepted": False,
        "reason": "The original criterion remains unmet",
        "criteria": [{
            "criterion": criterion,
            "met": False,
            "evidence_ids": [],
            "claims": [],
        } for criterion in mission.success_criteria],
    }


async def test_current_replan_exhaustion_is_stored_and_reported_after_reopen(tmp_path):
    async with selected_run(tmp_path) as (session, llm, tools, config, configured, directory):
        state = session.deps.central_state
        expected = await exhaust_replan_budget(session)
        await make_completion_review_fail(session, llm)

        final = await session.settle(session.runtime)

        assert final.termination_reason == TerminationReason.NO_ACTIONABLE_PLAN
        assert final.planning_diagnostic == expected.diagnostic
        run_id, engagement_id = str(final.run_id), str(final.engagement_id)
        path = state.path

    _, _, report = await inspect_central_run(path, run_id)
    assert report.termination_reason == TerminationReason.NO_ACTIONABLE_PLAN
    assert report.planning_diagnostic == expected.diagnostic
    from redteam.app.cli.commands.report import _render_markdown

    assert "planner_replan_exhausted" in _render_markdown(report)

    # Reopening the original Run reads the same mission-bound diagnostic; it does not
    # manufacture a new planner result or clear the safe exhaustion report.
    async with open_central_run(
        config,
        directory=directory,
        settings=configured,
        resume=True,
        run_id=run_id,
        engagement_id=engagement_id,
        llm=llm,
        tool_gateway=tools,
        tool_catalog=tools.catalog,
    ) as resumed:
        assert await resumed.deps.central_state.transaction(
            lambda tx: tx.planning_diagnostics(), write=False,
        ) == (expected.diagnostic, [expected.diagnostic])


async def test_a_new_planner_wake_makes_prior_replan_exhaustion_stale(tmp_path):
    async with selected_run(tmp_path) as (session, llm, *_):
        state = session.deps.central_state
        await exhaust_replan_budget(session)
        await state.transaction(lambda tx: tx.queue(event(
            state,
            RESULT_UPDATED,
            {"task_id": "new-original-result"},
        )))
        await make_completion_review_fail(session, llm)

        final = await session.settle(session.runtime)

        assert final.termination_reason == TerminationReason.INVESTIGATION_HELD
        assert final.planning_diagnostic is None
        assert await state.transaction(lambda tx: tx.planning_diagnostics(), write=False) == (
            None, [],
        )


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        (MissionStatus.COMPLETED, TerminationReason.GOAL_ACHIEVED),
        (MissionStatus.CANCELLED, TerminationReason.OPERATOR_STOPPED),
        (MissionStatus.PAUSED, TerminationReason.OPERATOR_PAUSED),
    ],
)
async def test_authoritative_goal_and_operator_statuses_win_over_exhaustion(
    tmp_path, status, reason,
):
    async with selected_run(tmp_path) as (session, llm, *_):
        state = session.deps.central_state
        await exhaust_replan_budget(session)

        def change_status(tx):
            mission_id, _ = tx.binding()
            current = tx.get_mission(mission_id)
            tx.update_mission(current.model_copy(update={
                "revision": current.revision + 1,
                "status": status,
                "termination_reason": reason,
            }), expected_revision=current.revision)

        await state.transaction(change_status)
        final = await session.settle(session.runtime)

        assert final.termination_reason == reason
        assert final.planning_diagnostic is None


async def test_goal_origin_cas_at_final_commit_is_stored_reported_and_reopened(
    tmp_path, monkeypatch,
):
    async with selected_run(tmp_path) as (session, llm, tools, config, configured, directory):
        originals = await finish_inconclusive(session, llm)
        state = session.deps.central_state
        source, = await state.list_tasks()
        await state.transaction(lambda tx: tx._write_task(
            tx.get_task(str(source.task_id)).model_copy(update={"required": False}),
        ))
        mission = await state.get_mission(session.runtime.mission_id)
        llm._canned_structured = {
            "accepted": True,
            "reason": "The optional original supports the goal criterion",
            "criteria": [{
                "criterion": criterion,
                "met": True,
                "evidence_ids": originals,
                "claims": [{
                    "kind": "semantic",
                    "description": "The preserved response supports the criterion",
                    "evidence_ids": originals,
                }],
            } for criterion in mission.success_criteria],
        }
        original_transaction = state.transaction

        async def change_source_inside_goal_commit(callback, *, write=True):
            code = getattr(callback, "__code__", None)
            if (write and code is not None and code.co_name == "commit"
                    and code.co_filename.endswith("central_completion.py")):
                def with_changed_source(tx):
                    current = tx.get_task(str(source.task_id))
                    tx._write_task(current.model_copy(update={"title": "Changed at goal commit"}))
                    return callback(tx)
                return await original_transaction(with_changed_source, write=write)
            return await original_transaction(callback, write=write)

        monkeypatch.setattr(state, "transaction", change_source_inside_goal_commit)
        try:
            final = await session.settle(session.runtime)
        finally:
            monkeypatch.setattr(state, "transaction", original_transaction)

        diagnostic = final.planning_diagnostic
        assert final.termination_reason == TerminationReason.INVESTIGATION_HELD
        assert diagnostic is not None
        assert diagnostic.code == GOAL_ORIGIN_CHANGED_CODE
        assert diagnostic.owner == "orchestration.central_completion"
        assert diagnostic.evidence_ids
        assert final.planning_diagnostic_history == [diagnostic]
        assert await state.transaction(lambda tx: tx.planning_diagnostics(), write=False) == (
            diagnostic, [diagnostic],
        )
        assert (await state.get_task(str(source.task_id))).title == source.title
        assert (await state.get_mission(session.runtime.mission_id)).status is not (
            MissionStatus.COMPLETED
        )
        run_id, engagement_id = str(final.run_id), str(final.engagement_id)
        path = state.path

    _, _, report = await inspect_central_run(path, run_id)
    assert report.termination_reason == TerminationReason.INVESTIGATION_HELD
    assert report.planning_diagnostic == diagnostic
    from redteam.app.cli.commands.report import _render_markdown

    assert GOAL_ORIGIN_CHANGED_CODE in _render_markdown(report)

    async with open_central_run(
        config,
        directory=directory,
        settings=configured,
        resume=True,
        run_id=run_id,
        engagement_id=engagement_id,
        llm=llm,
        tool_gateway=tools,
        tool_catalog=tools.catalog,
    ) as resumed:
        assert await resumed.deps.central_state.transaction(
            lambda tx: tx.planning_diagnostics(), write=False,
        ) == (diagnostic, [diagnostic])
