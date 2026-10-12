"""Plan 4c F07/F15: operator context for the planner and necessity validation.

Applied guidance, focus areas and reprioritize ordering reach the planner model
input from Mission preferences plus the applied intent ledger. Rejected intents
are never authorization, stale intents never override newer ones, and other Runs
never leak across. Necessity proposals must be grounded before they may touch
central state.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from redteam.core.contracts.mission import MissionPlan, MissionTask
from redteam.core.enums import MissionStatus, OperatorIntentType
from redteam.core.ids import new_id
from redteam.orchestration.operator_intents.models import IntentRecord, IntentStatus
from redteam.orchestration.specialist_planner import operator_planner_context
from redteam.orchestration.specialist_tasks import (
    SpecialistProposalError,
    TaskNecessityProposal,
    validate_necessity_proposals,
)

NOW = datetime(2026, 10, 1, tzinfo=timezone.utc)


def plan4c_mission(**overrides):
    payload = {
        "mission_id": "mission-plan4c",
        "engagement_id": "eng-plan4c",
        "objective": "Assess the authorized target",
        "success_criteria": ["Assess response using originals"],
        "status": MissionStatus.ACTIVE,
        "revision": 3,
        "created_at": NOW,
        "updated_at": NOW,
        "provenance": {"source": "test"},
        **overrides,
    }
    return MissionPlan.model_validate(payload)


def plan4c_record(intent_type, instruction, *, status=IntentStatus.APPLIED, run_id="run-plan4c",
                   created_at=NOW, task_ids=None, parameters=None, outcome="applied"):
    return IntentRecord.model_validate({
        "intent": {
            "intent_id": new_id("intent"),
            "mission_id": "mission-plan4c",
            "intent_type": intent_type.value,
            "instruction": instruction,
            "task_ids": task_ids or [],
            "parameters": parameters or {},
            "provenance": {"source": "test"},
            "created_at": created_at.isoformat(),
        },
        "status": status.value,
        "run_id": run_id,
        "correlation_id": "plan4c",
        "outcome": outcome,
    })


def plan4c_task(task_id="task-a", *, owner=None, version=0):
    payload = {
        "task_id": task_id,
        "mission_id": "mission-plan4c",
        "title": "probe",
        "objective": "probe the target",
        "provenance": {"source": "test"},
        "created_at": NOW.isoformat(),
        "updated_at": NOW.isoformat(),
    }
    if owner is not None:
        payload["occupant_owner"] = owner
        payload["ownership_version"] = version
    return MissionTask.model_validate(payload)


def plan4c_proposal(task_id="task-a", **overrides):
    payload = {
        "task_id": task_id,
        "reason": "Adds nothing to the stated criterion",
        "goal_revision": 3,
        "criterion_refs": ["Assess response using originals"],
        **overrides,
    }
    return TaskNecessityProposal.model_validate(payload)
def test_latest_applied_guidance_wins_and_rejected_is_ignored():
    first = plan4c_record(
        OperatorIntentType.GUIDANCE, "first guidance", created_at=NOW - timedelta(minutes=5))
    decoy = plan4c_record(
        OperatorIntentType.GUIDANCE, "decoy guidance", status=IntentStatus.REJECTED,
        outcome="rejected", created_at=NOW - timedelta(minutes=1))
    latest = plan4c_record(OperatorIntentType.GUIDANCE, "latest guidance", created_at=NOW)
    context = operator_planner_context(
        plan4c_mission(), [latest, decoy, first], run_id="run-plan4c")
    assert context["guidance"] == "latest guidance"
    assert [entry["intent_type"] for entry in context["applied_intents"]] == [
        "guidance", "guidance",
    ]


def test_other_run_intents_never_leak_across():
    foreign = plan4c_record(OperatorIntentType.GUIDANCE, "foreign guidance", run_id="run-other")
    mine = plan4c_record(OperatorIntentType.FOCUS, "focus web")
    context = operator_planner_context(plan4c_mission(), [foreign, mine], run_id="run-plan4c")
    assert context["guidance"] is None
    assert [entry["intent_type"] for entry in context["applied_intents"]] == ["focus"]


def test_preferences_and_ledger_stay_ordered_and_bounded():
    mission = plan4c_mission(operator_preferences={
        "focus_areas": ["web"],
        "prioritized_task_ids": ["task-b", "task-a"],
        "excluded_endpoints": ["/api/docs"],
    })
    intents = [
        plan4c_record(
            OperatorIntentType.FOCUS, "focus web", created_at=NOW - timedelta(minutes=2)),
        plan4c_record(
            OperatorIntentType.REPRIORITIZE, "task-b first",
            created_at=NOW - timedelta(minutes=1)),
        plan4c_record(
            OperatorIntentType.GUIDANCE, "extra", created_at=NOW - timedelta(seconds=30)),
    ]
    intents += [
        plan4c_record(OperatorIntentType.STATUS_QUERY, "status", created_at=NOW),
        plan4c_record(OperatorIntentType.STATUS_QUERY, "status", created_at=NOW),
        plan4c_record(OperatorIntentType.STATUS_QUERY, "status", created_at=NOW),
        plan4c_record(OperatorIntentType.STATUS_QUERY, "status", created_at=NOW),
    ]
    context = operator_planner_context(mission, intents, run_id="run-plan4c")
    assert context["focus_areas"] == ["web"]
    assert context["prioritized_task_ids"] == ["task-b", "task-a"]
    assert context["excluded_endpoints"] == ["/api/docs"]
    assert context["mission_revision"] == 3
    assert len(context["applied_intents"]) == 5
    assert context["applied_intents"][0]["intent_type"] == "guidance"
def check(proposals, *, mission=None, tasks=None, evidence=None):
    mission = mission or plan4c_mission()
    tasks = tasks if tasks is not None else {"task-a": plan4c_task("task-a")}
    return validate_necessity_proposals(
        proposals, mission=mission, tasks_by_id=tasks, evidence_ids=evidence)


def test_valid_cancel_and_set_required():
    tasks = {"task-a": plan4c_task("task-a"), "task-b": plan4c_task("task-b")}
    assert check([plan4c_proposal("task-a")], tasks=tasks) is None
    proposal = plan4c_proposal("task-b", action="set_required", required=False)
    assert check([proposal], tasks=tasks) is None


def test_unknown_task_stale_goal_and_duplicates_rejected():
    with pytest.raises(SpecialistProposalError) as unknown:
        check([plan4c_proposal("task-missing")])
    assert unknown.value.code == "planner_adjustment_unknown_task"
    with pytest.raises(SpecialistProposalError) as stale:
        check([plan4c_proposal("task-a", goal_revision=2)])
    assert stale.value.code == "planner_adjustment_stale_goal"
    with pytest.raises(SpecialistProposalError) as duplicate:
        check([plan4c_proposal("task-a"), plan4c_proposal("task-a")])
    assert duplicate.value.code == "planner_adjustment_duplicate_task"


def test_ungrounded_or_misreferenced_proposals_rejected():
    with pytest.raises(SpecialistProposalError) as ungrounded:
        check([plan4c_proposal("task-a", criterion_refs=[])])
    assert ungrounded.value.code == "planner_adjustment_ungrounded"
    with pytest.raises(SpecialistProposalError) as criterion:
        check([plan4c_proposal("task-a", criterion_refs=["Unknown criterion"])])
    assert criterion.value.code == "planner_adjustment_unknown_criterion"
    with pytest.raises(SpecialistProposalError) as link:
        check([plan4c_proposal(
            "task-a", criterion_refs=[], source_task_id="task-a",
            source_result_id="result-1", source_question_id="q1",
        )])
    assert link.value.code == "planner_adjustment_bad_result_link"
    with pytest.raises(SpecialistProposalError) as flag:
        check([plan4c_proposal("task-a", action="set_required")])
    assert flag.value.code == "planner_adjustment_missing_required_flag"
    with pytest.raises(SpecialistProposalError) as conflict:
        check([plan4c_proposal("task-a", action="cancel", required=True)])
    assert conflict.value.code == "planner_adjustment_cancel_keeps_required"


def test_owner_version_checked_against_holding_worker():
    owned = plan4c_task("task-a", owner="run:inst-1", version=2)
    tasks = {"task-a": owned}
    assert check([plan4c_proposal("task-a", owner="run:inst-1", version=2)], tasks=tasks) is None
    with pytest.raises(SpecialistProposalError) as stale_owner:
        check([plan4c_proposal("task-a", owner="run:inst-1", version=1)], tasks=tasks)
    assert stale_owner.value.code == "planner_adjustment_stale_owner"
    with pytest.raises(SpecialistProposalError) as unowned:
        check([plan4c_proposal("task-a", owner="run:inst-1", version=2)])
    assert unowned.value.code == "planner_adjustment_unowned_has_owner"


def test_evidence_grounding_requires_known_same_run_refs():
    assert check(
        [plan4c_proposal("task-a", evidence_ids=["evidence-real"], criterion_refs=[])],
        evidence={"evidence-real"},
    ) is None
    with pytest.raises(SpecialistProposalError) as forged:
        check(
            [plan4c_proposal("task-a", evidence_ids=["evidence-forged"], criterion_refs=[])],
            evidence={"evidence-real"},
        )
    assert forged.value.code == "planner_adjustment_unknown_evidence"


def test_source_result_link_checked_against_recorded_result():
    source = plan4c_task("task-src").model_copy(update={
        "result": SimpleNamespace(
            result_id="result-1", questions=[SimpleNamespace(question_id="q1")]),
    })
    tasks = {"task-a": plan4c_task("task-a"), "task-src": source}
    linked = plan4c_proposal(
        "task-a", criterion_refs=[], source_task_id="task-src",
        source_result_id="result-1", source_question_id="q1",
    )
    assert check([linked], tasks=tasks) is None
