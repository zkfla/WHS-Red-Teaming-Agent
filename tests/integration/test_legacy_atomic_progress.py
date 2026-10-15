"""Legacy single-planner atomic commit regressions: wake consumption, necessity batches, drift.

Real central assembly with FakeLLMClient doubles only. The planner judgment, Task
registration, necessity adjustment and at most one wake commit together; a planning input that
changes between the model decision and that commit refuses the whole batch.
"""

import json

from redteam.app.central_development import build_central_development
from redteam.app.central_run import bind_run_metadata
from redteam.core.enums import OperatorIntentType, TaskStatus
from redteam.llm.fake import FakeLLMClient
from redteam.orchestration.planner_progress import PLANNING_INVALIDATED
from redteam.orchestration.specialist_planner import SpecialistPlanner
from tests.integration.test_central_development import engagement, gateway, register, settings
from tests.integration.test_plan4c_planner import (
    apply_plan4c_intent,
    atomization_response,
    plan4c_intent,
    strategy_response,
)


def install_drift_after_decision(llm, hook):
    """Run one external-input hook after the atomization decision returns, before commit.

    The double stays an exact FakeLLMClient; only this instance's structured call is wrapped.
    """
    original = llm.complete_structured
    pending = [hook]

    async def drifting(*args, **kwargs):
        before = len(llm.requests)
        result = await original(*args, **kwargs)
        if before == 1 and pending:
            await pending.pop()()
        return result

    llm.complete_structured = drifting


def _outbox(tx):
    rows = tx.db.execute(
        "SELECT payload FROM state_outbox WHERE run_id=? ORDER BY sequence", (tx.run_id,),
    )
    return [json.loads(raw)["envelope"] for (raw,) in rows]


async def wake_payloads(state):
    envelopes = await state.transaction(_outbox, write=False)
    return [item["payload"] for item in envelopes if item["event_type"] == PLANNING_INVALIDATED]


async def task_ids(state):
    return [str(task.task_id) for task in await state.list_tasks()]


async def replan_accepted(state):
    return await state.transaction(lambda tx: tx.planner_replan_accepted(), write=False)


async def cursor(state):
    return await state.transaction(lambda tx: tx.hypothesis_planner_cursor(), write=False)


async def open_mission_with_two_tasks(selection):
    await bind_run_metadata(selection.deps)
    await register(selection, "registration-a")
    await register(selection, "registration-b")
    state = selection.deps.central_state
    mission = await state.get_mission(selection.runtime.mission_id)
    await state.update_mission(mission.model_copy(update={
        "success_criteria": ["Assess response"],
        "revision": mission.revision + 1,
    }))
    identities = sorted(await task_ids(state))
    return identities[-1], identities[0]


def cancel_adjustment(task_id, revision):
    return [{
        "task_id": task_id,
        "action": "cancel",
        "reason": "Extra probe adds nothing to the stated criterion",
        "goal_revision": revision,
        "criterion_refs": ["Assess response"],
    }]


async def test_nonempty_questions_commit_once_and_the_same_wake_does_not_repeat():
    llm = FakeLLMClient(structured_responses=[strategy_response(), atomization_response()])
    async with build_central_development(
        engagement(), llm=llm, tools=gateway(), settings=settings(),
    ) as selection:
        await bind_run_metadata(selection.deps)
        state = selection.deps.central_state
        planner = SpecialistPlanner(selection.deps)

        identities = await planner.advance(selection.runtime)
        assert len(identities) == 1
        assert len(llm.requests) == 2
        assert await task_ids(state) == identities
        status = await state.planner_progress_status()
        assert status is not None and status.kind == "work_proposed"
        assert await cursor(state) is not None

        # Same wake: no new model request, no second Task, no second judgment or wake.
        assert await planner.advance(selection.runtime) == []
        assert len(llm.requests) == 2
        assert await task_ids(state) == identities
        assert await state.planner_progress_status() == status
        assert await wake_payloads(state) == []


async def test_replan_cancel_commits_one_adjustment_and_one_wake_in_one_pass():
    llm = FakeLLMClient()
    async with build_central_development(
        engagement(), llm=llm, tools=gateway(), settings=settings(),
    ) as selection:
        target, other = await open_mission_with_two_tasks(selection)
        state = selection.deps.central_state
        mission = await state.get_mission(selection.runtime.mission_id)
        llm.structured_responses = [{
            "questions": [],
            "reason": "Request one bounded follow-up judgment",
            "termination": "replan",
            "task_adjustments": cancel_adjustment(target, mission.revision),
        }]

        assert await SpecialistPlanner(selection.deps).advance(selection.runtime) == []
        assert len(llm.requests) == 1

        cancelled = await state.get_task(target)
        assert cancelled.status is TaskStatus.CANCELLED
        adjustments = cancelled.provenance.metadata["necessity_adjustments"]
        assert [item["action"] for item in adjustments] == ["cancel"]
        assert (await state.get_task(other)).status is TaskStatus.READY
        assert len(await task_ids(state)) == 2
        assert await replan_accepted(state) == 1
        status = await state.planner_progress_status()
        assert status is not None and (status.kind, status.replan_count) == ("replan_queued", 1)
        assert len(await wake_payloads(state)) == 1


async def test_external_input_drift_after_decision_rejects_the_whole_batch():
    llm = FakeLLMClient()
    async with build_central_development(
        engagement(), llm=llm, tools=gateway(), settings=settings(),
    ) as selection:
        target, _ = await open_mission_with_two_tasks(selection)
        state = selection.deps.central_state
        mission = await state.get_mission(selection.runtime.mission_id)
        before = await task_ids(state)
        llm.structured_responses = [
            strategy_response(adjustments=cancel_adjustment(target, mission.revision)),
            atomization_response(),
        ]
        install_drift_after_decision(llm, lambda: apply_plan4c_intent(selection, plan4c_intent(
            selection, OperatorIntentType.GUIDANCE, instruction="external drift before commit")))

        assert await SpecialistPlanner(selection.deps).advance(selection.runtime) == []
        assert len(llm.requests) == 2
        # Nothing from the decision is written: no Task, no cancel, no judgment, no cursor.
        assert await task_ids(state) == before
        assert (await state.get_task(target)).status is TaskStatus.READY
        assert "necessity_adjustments" not in (await state.get_task(target)).provenance.metadata
        assert await replan_accepted(state) == 0
        assert await cursor(state) is None
        assert await state.planner_progress_status() is None
        # A refused guard writes nothing; the drifted input's own intent is the wake that reruns it.
        assert await wake_payloads(state) == []

        # The refused decision is not consumed, so the next pass commits its batch once.
        llm.structured_responses = [
            strategy_response(adjustments=cancel_adjustment(
                target, (await state.get_mission(selection.runtime.mission_id)).revision)),
            atomization_response(),
        ]
        identities = await SpecialistPlanner(selection.deps).advance(selection.runtime)
        assert len(identities) == 1
        assert len(await task_ids(state)) == len(before) + 1
        cancelled = await state.get_task(target)
        assert cancelled.status is TaskStatus.CANCELLED
        assert len(cancelled.provenance.metadata["necessity_adjustments"]) == 1
