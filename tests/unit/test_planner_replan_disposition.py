"""Planner continuation contracts; fixtures are pure fakes, with no live LLM or targets."""

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

import redteam.orchestration.agents.tool_access as tool_access
import redteam.orchestration.central_runtime as central_runtime
from redteam.attack_graph.identity import hypothesis_node_id
from redteam.core.contracts.completion import CompletionCriterion
from redteam.core.contracts.graph import CandidateHypothesis, HypothesisUpdate
from redteam.core.contracts.mission import MissionTask, Provenance
from redteam.core.contracts.specialists import ProbeLimits
from redteam.core.enums import TaskStatus
from redteam.core.ids import AgentId, MissionId, TaskId
from redteam.orchestration.coordination import content_hash
from redteam.orchestration.hypothesis_planner import (
    HypothesisDecision,
    HypothesisProposalError,
    PlanningSnapshot,
    _existing_work_witnesses,
    _proposal_repair_summary,
    bind_decision,
    related_reads,
)
from redteam.orchestration.probe_atomization import (
    AtomizationProposalError,
    HypothesisPlanDraft,
    PlannedInvestigationQuestion,
    bind_plan,
)
from redteam.orchestration.specialist_tasks import ProbeTaskProposal

NOW = datetime(2026, 10, 14, tzinfo=timezone.utc)
ROLE = "probe-worker"
TARGET = "host:a"


def _mission(*, excluded=()):
    return SimpleNamespace(operator_preferences={"excluded_task_ids": list(excluded)})


def _candidate(identity="candidate-a", revision=1):
    return CandidateHypothesis(
        contract_version=2, hypothesis_id=identity, description="Access may be unguarded",
        target_node_ids=[TARGET], reason="Assess the observed response", revision=revision,
    )


def _task(
    identity="task-a", *, status=TaskStatus.READY, role=ROLE, owner=None,
    revisions=None, probe_limits=True, blocking_reason=None,
):
    revisions = dict(revisions or {})
    bounded = probe_limits or bool(revisions)
    return MissionTask(
        task_id=TaskId(identity), mission_id=MissionId("mission-a"),
        title="Observe one response", objective="Inspect the captured response",
        completion_contract_version=2 if bounded else 1,
        investigation_contract_version=2 if bounded else 1,
        probe_limits=ProbeLimits() if probe_limits else None,
        completion_criteria=([CompletionCriterion(
            kind="observation", description="Inspect the response",
        )] if bounded else []),
        related_hypothesis_ids=list(revisions), hypothesis_revisions=revisions,
        assigned_agent=AgentId(role), status=status, occupant_owner=owner,
        ownership_version=1 if owner else 0, blocking_reason=blocking_reason,
        provenance=Provenance(source="planner-test"), created_at=NOW, updated_at=NOW,
    )


def _snapshot(*tasks, candidates=(), mission=None):
    task_map = {str(task.task_id): task for task in tasks}
    candidate_map = {item.hypothesis_id: item for item in candidates}
    return PlanningSnapshot(
        mission=mission or _mission(), tasks=task_map, nodes={}, candidates=candidate_map,
        binding="binding-a", sequence=1, consumed=None,
    )


def _update(candidate):
    return HypothesisUpdate(
        hypothesis=candidate.model_copy(update={"description": "Revised captured premise"}),
        expected_revision=candidate.revision,
    )


def _witness_validator(snapshot, roles=None, *, require_probe_limits=True):
    roles = {ROLE} if roles is None else roles
    def validate(task_ids, projected_revisions):
        return _existing_work_witnesses(
            task_ids, snapshot, eligible_roles=roles,
            require_probe_limits=require_probe_limits,
            candidate_revisions=projected_revisions,
        )

    return validate


def _bind_legacy(decision, snapshot, *, roles=None):
    roles = {ROLE} if roles is None else roles
    return bind_decision(
        decision, snapshot, issued={}, targets={TARGET}, principals=set(), roles=roles,
        max_tasks=4, limits=ProbeLimits(),
    )


def _plan_draft(*, updates=(), questions=(), termination="continue", task_ids=(),
                reason="Continue with this purpose", resume_conditions=()):
    return HypothesisPlanDraft.model_validate({
        "updates": list(updates), "questions": list(questions),
        "reviewed_task_ids": list(task_ids), "reason": reason,
        "termination": termination, "resume_conditions": list(resume_conditions),
    })


def _bind_split(draft, snapshot, *, roles=None):
    roles = {ROLE} if roles is None else roles
    return bind_plan(
        draft, snapshot, issued={}, targets={TARGET}, principals=set(), max_tasks=4,
        existing_work_validator=_witness_validator(snapshot, roles),
    )


def test_candidate_only_continue_repairs_in_both_judgment_schemas():
    candidate = _candidate()
    snapshot = _snapshot(candidates=[candidate])
    update = _update(candidate)

    legacy = HypothesisDecision(updates=[update], reason="Revise the premise")
    with pytest.raises(HypothesisProposalError) as legacy_error:
        _bind_legacy(legacy, snapshot)
    assert legacy_error.value.code == "continue_requires_proposed_or_existing_work"
    assert _bind_legacy(
        legacy.model_copy(update={"termination": "replan"}), snapshot,
    ) is not None

    split = _plan_draft(updates=[update], reason="Revise the premise")
    with pytest.raises(AtomizationProposalError) as split_error:
        _bind_split(split, snapshot)
    assert split_error.value.code == "continue_requires_proposed_or_existing_work"
    bound, questions, _ = _bind_split(
        split.model_copy(update={"termination": "replan"}), snapshot,
    )
    assert bound and not questions


def test_continue_accepts_new_tasks_or_questions():
    snapshot = _snapshot()
    task = ProbeTaskProposal.model_validate({
        "task_key": "probe-a", "title": "Observe response", "objective": "Inspect response",
        "role_id": ROLE, "target_node_ids": [TARGET],
        "completion_criteria": [{"kind": "observation", "description": "Inspect response"}],
    })
    proposal = _bind_legacy(
        HypothesisDecision(tasks=[task], reason="A new probe is ready"), snapshot,
    )
    assert proposal is not None and proposal.tasks == [task]

    question = PlannedInvestigationQuestion.model_validate({
        "question_id": "q-a", "objective": "Inspect the response", "target_node_ids": [TARGET],
        "completion_criteria": [{"kind": "observation", "description": "Inspect response"}],
    })
    updates, questions, _ = _bind_split(
        _plan_draft(questions=[question.model_dump(mode="json")]), snapshot,
    )
    assert updates == [] and len(questions) == 1


def test_empty_inventory_requires_candidate_only_for_captured_continuing_investigation():
    snapshot = _snapshot()
    legacy = HypothesisDecision(
        reason="No candidate was proposed", termination="held",
        resume_conditions=["A captured target observation becomes available"],
    )
    assert bind_decision(
        legacy, snapshot, issued={}, targets={TARGET}, principals=set(), roles={ROLE},
        max_tasks=4, limits=ProbeLimits(), require_initial_candidate=True,
    ) is None

    split = _plan_draft(
        reason="No candidate was proposed", termination="held",
        resume_conditions=["A captured target observation becomes available"],
    )
    updates, questions, _ = bind_plan(
        split, snapshot, issued={}, targets={TARGET}, principals=set(), max_tasks=4,
        existing_work_validator=_witness_validator(snapshot), require_initial_candidate=True,
    )
    assert updates is None and questions == []

    task = ProbeTaskProposal.model_validate({
        "task_key": "probe-initial", "title": "Observe response", "objective": "Inspect response",
        "role_id": ROLE, "target_node_ids": [TARGET],
        "completion_criteria": [{"kind": "observation", "description": "Inspect response"}],
    })
    with pytest.raises(HypothesisProposalError) as legacy_error:
        bind_decision(
            HypothesisDecision(tasks=[task], reason="Continue the captured investigation"),
            snapshot, issued={}, targets={TARGET}, principals=set(), roles={ROLE},
            max_tasks=4, limits=ProbeLimits(), require_initial_candidate=True,
        )
    assert legacy_error.value.code == "candidate_inventory_empty_requires_new_candidate"

    question = PlannedInvestigationQuestion.model_validate({
        "question_id": "q-initial", "objective": "Inspect the response",
        "target_node_ids": [TARGET],
        "completion_criteria": [{"kind": "observation", "description": "Inspect response"}],
    })
    with pytest.raises(AtomizationProposalError) as split_error:
        bind_plan(
            _plan_draft(questions=[question.model_dump(mode="json")]), snapshot,
            issued={}, targets={TARGET}, principals=set(), max_tasks=4,
            existing_work_validator=_witness_validator(snapshot), require_initial_candidate=True,
        )
    assert split_error.value.code == "candidate_inventory_empty_requires_new_candidate"

    new_candidate = _candidate("proposal-local")
    update = HypothesisUpdate(hypothesis=new_candidate, expected_revision=None)
    task = task.model_copy(update={
        "hypothesis_revisions": {"proposal-local": 1},
    })
    accepted = bind_decision(
        HypothesisDecision(
            updates=[update], tasks=[task], reason="Continue the captured investigation",
        ),
        snapshot,
        issued={},
        targets={TARGET},
        principals=set(),
        roles={ROLE},
        max_tasks=4,
        limits=ProbeLimits(),
        require_initial_candidate=True,
    )
    assert accepted is not None and len(accepted.updates) == 1


@pytest.mark.parametrize("status,owner", [
    (TaskStatus.READY, None),
    (TaskStatus.RUNNING, "run-a:instance:worker-a"),
])
def test_referenced_claimable_or_running_witness_is_accepted_and_cas_read(status, owner):
    task = _task(status=status, owner=owner)
    snapshot = _snapshot(task)
    decision = HypothesisDecision(
        reviewed_task_ids=[str(task.task_id)], reason="Continue the existing probe",
    )

    assert _bind_legacy(decision, snapshot) is None
    _, task_read = related_reads(decision, None, snapshot, [])
    assert task_read == {str(task.task_id): content_hash(task)}


@pytest.mark.parametrize("surface", ["legacy", "split"])
def test_candidate_update_invalidates_old_revision_ready_witness(surface):
    candidate = _candidate(revision=1)
    task = _task(revisions={candidate.hypothesis_id: 1})
    snapshot = _snapshot(task, candidates=[candidate])
    update = _update(candidate)
    if surface == "legacy":
        decision = HypothesisDecision(
            updates=[update], reviewed_task_ids=[str(task.task_id)],
            reason="Revise candidate while continuing",
        )
        with pytest.raises(HypothesisProposalError) as caught:
            _bind_legacy(decision, snapshot)
    else:
        draft = _plan_draft(
            updates=[update], task_ids=[str(task.task_id)],
            reason="Revise candidate while continuing",
        )
        with pytest.raises(AtomizationProposalError) as caught:
            _bind_split(draft, snapshot)
    assert caught.value.code == "continue_requires_proposed_or_existing_work"


@pytest.mark.parametrize("case", [
    "readiness_unavailable", "suppressed", "candidate_revision_mismatch",
    "missing_probe_limits", "role_mismatch", "approval_blocked",
])
def test_unavailable_or_nonexecutable_work_is_not_a_witness(case):
    candidate = _candidate(revision=2) if case == "candidate_revision_mismatch" else None
    if case == "suppressed":
        task = _task()
        mission = _mission(excluded=[str(task.task_id)])
    elif case == "candidate_revision_mismatch":
        task = _task(revisions={candidate.hypothesis_id: 1})
        mission = _mission()
    elif case == "missing_probe_limits":
        task = _task(probe_limits=False)
        mission = _mission()
    elif case == "role_mismatch":
        task = _task(role="unavailable-role")
        mission = _mission()
    elif case == "approval_blocked":
        task = _task(status=TaskStatus.BLOCKED, blocking_reason="approval:pending")
        mission = _mission()
    else:
        task = _task()
        mission = _mission()
    snapshot = _snapshot(task, candidates=[candidate] if candidate else [], mission=mission)
    decision = HypothesisDecision(
        reviewed_task_ids=[str(task.task_id)], reason="Try the referenced work",
    )
    roles = set() if case == "readiness_unavailable" else {ROLE}

    with pytest.raises(HypothesisProposalError) as caught:
        _bind_legacy(decision, snapshot, roles=roles)

    assert caught.value.code == "continue_requires_proposed_or_existing_work"
    if case == "approval_blocked":
        assert task.blocking_reason.startswith("approval:")


def test_running_witness_keeps_captured_purpose_when_candidate_is_revised():
    candidate = _candidate(revision=1)
    task = _task(status=TaskStatus.RUNNING, owner="run-a:instance:worker-a",
                 revisions={candidate.hypothesis_id: 1})
    snapshot = _snapshot(task, candidates=[candidate])
    decision = HypothesisDecision(
        updates=[_update(candidate)], reviewed_task_ids=[str(task.task_id)],
        reason="Revise candidate while its original task continues",
    )

    proposal = _bind_legacy(decision, snapshot)

    assert proposal is not None and len(proposal.updates) == 1
    assert task.hypothesis_revisions == {candidate.hypothesis_id: 1}


def test_replan_allows_candidate_or_empty_purpose_but_never_new_work():
    candidate = _candidate()
    snapshot = _snapshot(candidates=[candidate])
    assert _bind_legacy(HypothesisDecision(
        updates=[_update(candidate)], reason="Revisit this candidate", termination="replan",
    ), snapshot) is not None
    assert _bind_legacy(HypothesisDecision(
        reason="Ask for one bounded follow-up", termination="replan",
    ), snapshot) is None

    question = {"question_id": "q-a", "objective": "Inspect response",
                "target_node_ids": [TARGET], "completion_criteria": [
                    {"kind": "observation", "description": "Inspect response"}]}
    with pytest.raises(AtomizationProposalError, match="new_work_requires_continue_judgment"):
        _bind_split(_plan_draft(questions=[question], termination="replan"), snapshot)
    with pytest.raises(HypothesisProposalError, match="new_work_requires_continue_judgment"):
        _bind_legacy(HypothesisDecision(
            tasks=[ProbeTaskProposal.model_validate({
                "task_key": "probe-a", "title": "Probe", "objective": "Inspect",
                "role_id": ROLE, "target_node_ids": [TARGET],
                "completion_criteria": [{"kind": "observation", "description": "Inspect"}],
            })], reason="Do not add tasks to replan", termination="replan",
        ), snapshot)


@pytest.mark.parametrize("surface", ["legacy", "split"])
def test_empty_goal_achieved_is_a_proposal_without_resume_condition(surface):
    snapshot = _snapshot()
    if surface == "legacy":
        assert _bind_legacy(HypothesisDecision(
            reason="The proposed objective may be met", termination="goal_achieved",
        ), snapshot) is None
        for termination in ("held", "exhausted"):
            with pytest.raises(HypothesisProposalError):
                _bind_legacy(HypothesisDecision(
                    reason="Wait for new inputs", termination=termination,
                ), snapshot)
    else:
        draft = _plan_draft(reason="The proposed objective may be met",
                            termination="goal_achieved")
        assert _bind_split(draft, snapshot)[0] is None
        for termination in ("held", "exhausted"):
            with pytest.raises(AtomizationProposalError):
                _bind_split(_plan_draft(reason="Wait for new inputs", termination=termination),
                            snapshot)


def test_replan_related_reads_stay_candidate_local_and_diagnostic_names_dispositions():
    witness, sibling = _task("witness"), _task("sibling")
    snapshot = _snapshot(witness, sibling)
    decision = HypothesisDecision(
        reviewed_task_ids=[str(witness.task_id)], reason="Request a bounded replan",
        termination="replan",
    )

    _, task_read = related_reads(decision, None, snapshot, [])
    assert task_read == {str(witness.task_id): content_hash(witness)}
    assert str(sibling.task_id) not in task_read

    summary = _proposal_repair_summary(
        HypothesisProposalError("continue_requires_proposed_or_existing_work"), "fallback",
    )
    for disposition in ("continue", "replan", "held", "exhausted", "goal_achieved"):
        assert disposition in summary
    assert "candidate updates alone are not progress" in summary


class _FakeRuntime:
    run_id = "run-a"
    engagement_id = "engagement-a"
    mission_id = "mission-a"
    current_goal = None
    started_at = NOW
    component_binding = None
    llm_request_policy = None
    remaining_budget = None

    def model_copy(self, *, deep=False, update=None):
        return SimpleNamespace(**(update or {}))


class _FakeGraph:
    def __init__(self, nodes):
        self.nodes = nodes

    def get_local_node(self, identity):
        return self.nodes.get(str(identity))


class _FakeTransaction:
    def __init__(self, mission, tasks, nodes):
        self.mission = mission
        self.tasks = {str(task.task_id): task for task in tasks}
        self.graph = _FakeGraph(nodes)

    def admit(self):
        return self.mission

    def active_activation_count(self):
        return 0

    def get_task(self, identity):
        return self.tasks.get(identity)

    def claim_task(self, identity, *, owner, instance_id):
        task = self.tasks[identity].model_copy(update={
            "status": TaskStatus.RUNNING, "occupant_owner": owner,
            "ownership_version": self.tasks[identity].ownership_version + 1,
        })
        self.tasks[identity] = task
        return task


class _FakeState:
    def __init__(self, mission, tasks, nodes):
        self.tx = _FakeTransaction(mission, tasks, nodes)
        self.tasks = tasks

    async def list_tasks(self):
        return list(self.tasks)

    async def transaction(self, callback):
        return callback(self.tx)


class _FakeDeps:
    def __init__(self, mission, tasks, nodes):
        self.central_state = _FakeState(mission, tasks, nodes)
        self.settings = SimpleNamespace(global_concurrency=10)
        self.specialist_policy = SimpleNamespace(max_instances=10, probe_limits=ProbeLimits())


async def test_ready_workers_uses_the_shared_claimability_predicate_on_one_snapshot(monkeypatch):
    candidate = _candidate(revision=2)
    node = SimpleNamespace(properties={"facts": candidate.model_dump(mode="json")})
    nodes = {str(hypothesis_node_id(candidate.hypothesis_id)): node}
    mission = _mission(excluded=["suppressed"])
    tasks = [
        _task("ready"),
        _task("role-mismatch", role="unavailable-role"),
        _task("suppressed"),
        _task("missing-limits", probe_limits=False),
        _task("approval", status=TaskStatus.BLOCKED, blocking_reason="approval:pending"),
        _task("stale-revision", revisions={candidate.hypothesis_id: 1}),
        _task("matching-revision", revisions={candidate.hypothesis_id: 2}),
    ]
    deps = _FakeDeps(mission, tasks, nodes)
    eligible = {ROLE}
    expected = {
        str(task.task_id) for task in tasks
        if central_runtime.task_is_claimable(
            task, eligible_roles=eligible, mission=mission, require_probe_limits=True,
            candidate_revisions={candidate.hypothesis_id: candidate.revision},
        )
    }
    called = []
    predicate = central_runtime.task_is_claimable

    def track(task, **kwargs):
        called.append(str(task.task_id))
        return predicate(task, **kwargs)

    async def no_controls(_deps):
        return None

    async def ready_roles(_deps):
        return eligible

    monkeypatch.setattr(central_runtime, "task_is_claimable", track)
    monkeypatch.setattr(central_runtime, "process_controls", no_controls)
    monkeypatch.setattr(tool_access, "ready_role_ids", ready_roles)

    workers = await central_runtime.ready_workers(deps, _FakeRuntime())
    returned = {str(worker.active_task_id) for worker in workers}
    assert set(called) == {str(task.task_id) for task in tasks}
    assert returned == expected == {"ready", "matching-revision"}
