"""Plan 4c F07/F08/F14/F15: planner input, reprioritize, time admission, necessity.

Real central assembly with explicit FakeLLM doubles only: applied operator
guidance reaches the strategy/atomization model input, reprioritize orders real
task selection, a duration-0 Run makes zero model requests, and planner
cancellation lifts the completion gate for unneeded tasks while genuinely
required work still gates.
"""

import json
from datetime import datetime, timezone

import pytest

from redteam.app.central_development import build_central_development
from redteam.core.enums import OperatorIntentType, TaskStatus
from redteam.core.ids import TaskId, new_id
from redteam.llm.fake import FakeLLMClient
from redteam.orchestration.central_completion import goal_inputs
from redteam.orchestration.central_runtime import process_controls, ready_workers
from redteam.orchestration.cognition import DecisionFailure
from redteam.orchestration.operator_intents.models import IntentRecord, IntentStatus
from redteam.orchestration.specialist_planner import SpecialistPlanner
from tests.integration.test_central_development import (
    TARGET,
    engagement,
    gateway,
    register,
    settings,
)


def plan4c_intent(selection, intent_type, *, task_ids=(), parameters=None, instruction=None):
    note = instruction if instruction is not None else "plan4c note"
    return IntentRecord.model_validate({
        "intent": {
            "intent_id": new_id("intent"),
            "mission_id": str(selection.runtime.mission_id),
            "intent_type": intent_type.value,
            "instruction": note,
            "task_ids": [str(task) for task in task_ids],
            "parameters": parameters or {},
            "provenance": {"source": "test"},
            "created_at": datetime.now(timezone.utc).isoformat(),
        },
        "status": IntentStatus.RECEIVED.value,
        "run_id": str(selection.deps.central_state.run_id),
        "correlation_id": "plan4c",
    })


async def apply_plan4c_intent(selection, record):
    await selection.deps.central_state.append(record)
    await process_controls(selection.deps)
    saved = await selection.deps.central_state.get(record.intent.intent_id)
    assert saved is not None
    return saved


def strategy_response(*, adjustments=None):
    payload = {
        "questions": [{
            "question_id": "q1",
            "objective": "Investigate response",
            "target_node_ids": [TARGET],
            "completion_criteria": [{"kind": "semantic", "description": "Assess response"}],
        }],
        "reason": "plan4c probe",
    }
    if adjustments is not None:
        payload["task_adjustments"] = adjustments
    return payload


def halted_response(*, adjustments=None):
    payload = {"questions": [], "reason": "plan4c halt", "termination": "exhausted"}
    if adjustments is not None:
        payload["task_adjustments"] = adjustments
    return payload


def atomization_response():
    return {"tasks": [{
        "task_key": "plan4c-task",
        "question_id": "q1",
        "objective": "Investigate response",
        "title": "Plan4c investigation",
        "role_id": "web-executor",
        "target_node_ids": [TARGET],
        "completion_criteria": [{"kind": "semantic", "description": "Assess response"}],
    }]}


def request_bodies(llm):
    bodies = []
    for entry in llm.requests:
        content = entry["messages"][0]["content"]
        bodies.append(json.loads(content.split("\n\nPrevious attempt failed")[0]))
    return bodies
async def test_guidance_focus_and_reprioritize_reach_model_input():
    llm = FakeLLMClient(structured_responses=[strategy_response(), atomization_response()])
    async with build_central_development(
        engagement(), llm=llm, tools=gateway(), settings=settings(),
    ) as selection:
        await register(selection)
        guidance = await apply_plan4c_intent(selection, plan4c_intent(
            selection, OperatorIntentType.GUIDANCE, instruction="prefer quiet reads first"))
        assert guidance.status is IntentStatus.APPLIED
        focus = await apply_plan4c_intent(selection, plan4c_intent(
            selection, OperatorIntentType.FOCUS, parameters={"focus_areas": ["web"]}))
        assert focus.status is IntentStatus.APPLIED
        tasks = await selection.deps.central_state.list_tasks()
        reprioritize = await apply_plan4c_intent(selection, plan4c_intent(
            selection, OperatorIntentType.REPRIORITIZE, task_ids=[tasks[0].task_id]))
        assert reprioritize.status is IntentStatus.APPLIED

        decoy = plan4c_intent(selection, OperatorIntentType.GUIDANCE, instruction="decoy-dropped")
        await selection.deps.central_state.append(decoy)
        await selection.deps.central_state.update(
            decoy.model_copy(update={"status": IntentStatus.REJECTED, "outcome": "rejected"}))

        await SpecialistPlanner(selection.deps).advance(selection.runtime)
        assert llm.requests, "a healthy run must issue model requests"
        bodies = request_bodies(llm)
        assert bodies[0]["operator_guidance"]["guidance"] == "prefer quiet reads first"
        assert bodies[0]["operator_guidance"]["focus_areas"] == ["web"]
        assert str(tasks[0].task_id) in bodies[0]["operator_guidance"]["prioritized_task_ids"]
        assert bodies[0]["mission_revision"] >= 1
        assert "decoy-dropped" not in json.dumps(bodies)


async def test_reprioritize_orders_real_selection_and_rejects_unknown():
    async with build_central_development(
        engagement(), llm=FakeLLMClient(), tools=gateway(), settings=settings(),
    ) as selection:
        await register(selection, "registration-a")
        await register(selection, "registration-b")
        tasks = await selection.deps.central_state.list_tasks()
        assert len(tasks) == 2
        last = sorted(str(task.task_id) for task in tasks)[-1]
        applied = await apply_plan4c_intent(selection, plan4c_intent(
            selection, OperatorIntentType.REPRIORITIZE, task_ids=[TaskId(last)]))
        assert applied.status is IntentStatus.APPLIED
        workers = await ready_workers(selection.deps, selection.runtime, max_instances=1)
        assert [str(worker.active_task_id) for worker in workers] == [last]

        before = [str(task.task_id) for task in await selection.deps.central_state.list_tasks()]
        rejected = await apply_plan4c_intent(selection, plan4c_intent(
            selection, OperatorIntentType.REPRIORITIZE, task_ids=[TaskId("task-missing")]))
        assert rejected.status is IntentStatus.REJECTED
        assert rejected.reason == "operator_reprioritize_unknown_task"
        after = [str(task.task_id) for task in await selection.deps.central_state.list_tasks()]
        assert before == after
async def test_duration_zero_run_makes_no_model_requests():
    llm = FakeLLMClient(structured_responses=[strategy_response(), atomization_response()])
    base = engagement()
    capped = base.execution_budget.model_copy(update={"max_duration_seconds": 0})
    eng = base.model_copy(update={"execution_budget": capped})
    async with build_central_development(
        eng, llm=llm, tools=gateway(), settings=settings(),
    ) as selection:
        await register(selection)
        with pytest.raises(DecisionFailure) as failed:
            await SpecialistPlanner(selection.deps).advance(selection.runtime)
        assert failed.value.kind == "budget"
        assert llm.requests == []


async def test_mid_sequence_exhaustion_is_structured_stop(monkeypatch):
    llm = FakeLLMClient(structured_responses=[strategy_response(), atomization_response()])
    async with build_central_development(
        engagement(), llm=llm, tools=gateway(), settings=settings(),
    ) as selection:
        await register(selection)
        import redteam.orchestration.reasoning_time as reasoning_time

        real_failure = reasoning_time.reasoning_time_failure
        calls = []

        def flaky(engagement, runtime, *, now=None):
            calls.append(1)
            if len(calls) >= 3:
                return ("reasoning_duration_exhausted", "budget_exhausted")
            return real_failure(engagement, runtime, now=now)

        monkeypatch.setattr(reasoning_time, "reasoning_time_failure", flaky)
        with pytest.raises(DecisionFailure) as failed:
            await SpecialistPlanner(selection.deps).advance(selection.runtime)
        assert failed.value.kind == "budget"
        assert len(llm.requests) == 1
async def test_planner_cancel_unblocks_optional_but_required_still_gates():
    llm = FakeLLMClient()
    async with build_central_development(
        engagement(), llm=llm, tools=gateway(), settings=settings(),
    ) as selection:
        await register(selection, "registration-a")
        await register(selection, "registration-b")
        state = selection.deps.central_state
        mission = await state.get_mission(selection.runtime.mission_id)
        await state.update_mission(mission.model_copy(update={
            "success_criteria": ["Assess response"],
            "revision": mission.revision + 1,
        }))
        mission = await state.get_mission(selection.runtime.mission_id)
        tasks = await state.list_tasks()
        target = sorted(str(task.task_id) for task in tasks)[-1]
        other = sorted(str(task.task_id) for task in tasks)[0]
        llm.structured_responses = [halted_response(adjustments=[{
            "task_id": target,
            "action": "cancel",
            "reason": "Extra probe adds nothing to the stated criterion",
            "goal_revision": mission.revision,
            "criterion_refs": ["Assess response"],
        }])]
        await SpecialistPlanner(selection.deps).advance(selection.runtime)
        cancelled = await state.get_task(target)
        assert cancelled.status is TaskStatus.CANCELLED
        assert cancelled.required is False
        assert cancelled.provenance.metadata["original_required"] is True
        assert cancelled.provenance.metadata["necessity_adjustments"][0]["action"] == "cancel"
        assert cancelled.provenance.metadata["necessity_adjustments"][0]["criteria"] == [
            "Assess response"]
        _, _, _, errors, _, _, _ = await goal_inputs(selection.deps, selection.runtime)
        assert "required_investigation_unfinished" in errors

        remaining = await state.get_mission(selection.runtime.mission_id)
        llm.structured_responses = [halted_response(adjustments=[{
            "task_id": other,
            "action": "set_required",
            "required": False,
            "reason": "The stated criterion is covered without this probe",
            "goal_revision": remaining.revision,
            "criterion_refs": ["Assess response"],
        }])]
        await SpecialistPlanner(selection.deps).advance(selection.runtime)
        optional = await state.get_task(other)
        assert optional.required is False
        _, _, _, relaxed, _, _, _ = await goal_inputs(selection.deps, selection.runtime)
        assert "required_investigation_unfinished" not in relaxed

        excluded = await apply_plan4c_intent(selection, plan4c_intent(
            selection, OperatorIntentType.EXCLUDE, task_ids=[TaskId(other)]))
        assert excluded.status is IntentStatus.APPLIED
        prefs = (await state.get_mission(selection.runtime.mission_id)).operator_preferences
        assert other in prefs.get("excluded_task_ids", [])
        workers = await ready_workers(selection.deps, selection.runtime, max_instances=5)
        claimed = {str(worker.active_task_id) for worker in workers}
        assert other not in claimed


async def test_necessity_grounding_revalidated_in_transaction():
    from types import SimpleNamespace

    from redteam.orchestration.coordination import StateConflict
    from redteam.orchestration.specialist_tasks import (
        TaskNecessityProposal,
        validate_necessity_proposals,
    )

    async with build_central_development(
        engagement(), llm=FakeLLMClient(), tools=gateway(), settings=settings(),
    ) as selection:
        await register(selection, "registration-a")
        await register(selection, "registration-b")
        state = selection.deps.central_state
        mission = await state.get_mission(selection.runtime.mission_id)
        await state.update_mission(mission.model_copy(update={
            "success_criteria": ["Assess response"],
            "revision": mission.revision + 1,
        }))
        mission = await state.get_mission(selection.runtime.mission_id)
        tasks = await state.list_tasks()
        target = sorted(str(task.task_id) for task in tasks)[-1]
        other = sorted(str(task.task_id) for task in tasks)[0]

        with pytest.raises(StateConflict, match="evidence_reference_not_inspected"):
            await state.adjust_task_necessity(
                target, action="cancel", reason="forged grounding",
                goal_revision=mission.revision, criterion_refs=["Assess response"],
                evidence_ids=["evidence-forged"],
            )

        with pytest.raises(StateConflict, match="bad_result_link"):
            await state.adjust_task_necessity(
                target, action="cancel", reason="stale link",
                goal_revision=mission.revision, criterion_refs=["Assess response"],
                source_task_id=other, source_result_id="result-9", source_question_id="q9",
            )

        raced_source = (await state.get_task(other)).model_copy(update={
            "result": SimpleNamespace(
                result_id="result-9", questions=[SimpleNamespace(question_id="q9")]),
        })
        proposal = TaskNecessityProposal.model_validate({
            "task_id": target, "action": "cancel", "reason": "race",
            "goal_revision": mission.revision, "criterion_refs": [],
            "source_task_id": other, "source_result_id": "result-9",
            "source_question_id": "q9",
        })
        live = {str(task.task_id): task for task in await state.list_tasks()}
        raced = dict(live)
        raced[other] = raced_source
        assert validate_necessity_proposals([proposal], mission=mission, tasks_by_id=raced) is None
        with pytest.raises(StateConflict, match="bad_result_link"):
            await state.adjust_task_necessity(
                target, action="cancel", reason="race",
                goal_revision=mission.revision,
                source_task_id=other, source_result_id="result-9", source_question_id="q9",
            )

        assert (await state.get_task(target)).status is TaskStatus.READY
