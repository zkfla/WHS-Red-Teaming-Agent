"""R3: unresolved settlement bypasses the review-budget gate."""

from __future__ import annotations

from redteam.attack_graph.observation import GraphObservation
from redteam.core.enums import MissionStatus, TaskStatus
from redteam.orchestration.cognition import DecisionBudget, DecisionFailure
from redteam.orchestration.coordination import coordinator_for
from redteam.orchestration.engine import InvestigationEngine
from redteam.orchestration.paths import PathPlan
from redteam.orchestration.settings import OrchestrationSettings
from tests.integration import _engine_llm_harness as h


async def _setup(task_status: TaskStatus = TaskStatus.RUNNING):
    llm = h.ScriptedLLM()
    seed = await h.seed_host("run-unresolved-budget")
    world = h.make_world(llm, graph_store=seed.store)
    # Real budget contract: normal 1 + repair 1. The next_action decision
    # itself spends the only normal permit, so the completion gate below
    # runs with the normal lane exhausted and only the repair lane left.
    world.deps.settings = OrchestrationSettings(
        decision_normal_limit=1, decision_max_attempts=1
    )
    mission, task = h.make_mission_task(
        mission_id="mission-unresolved-budget", engagement_id="eng-t1", criteria=["actual response"]
    )
    mission.status = MissionStatus.ACTIVE
    task.status = task_status
    await world.tasks.create_mission(mission)
    await world.tasks.create_task(task)
    runtime = h.make_runtime(
        "run-unresolved-budget", "eng-t1",
        mission_id="mission-unresolved-budget", task_id=str(task.task_id),
    )
    await coordinator_for(world.tasks, world.paths).bind(
        str(runtime.run_id), str(mission.mission_id), str(mission.engagement_id)
    )
    raw = h.draft_path(seed.node_id)
    raw.update(
        path_id="path-unresolved-budget",
        run_id=str(runtime.run_id),
        version=1,
        status="active",
        mission_id=str(mission.mission_id),
        mission_task_id=str(task.task_id),
        goal=task.objective,
    )
    path = await world.paths.register(PathPlan.model_validate(raw))
    await world.evidence.append(
        h.make_tool_evidence(
            evidence_id="proof-budget",
            run_id=str(runtime.run_id),
            engagement_id="eng-t1",
            action_id="action-budget",
            invocation_id="inv-budget",
            path_id=path.path_id,
            path_version=path.version,
            step_id="step-1",
            target_node_id=seed.node_id,
        )
    )
    return world, runtime, path, llm


def _routes(llm) -> list[str]:
    return [call["route"] for call in llm.calls]


def _settled_runtime(engine, runtime):
    # decide() may continue on a copied runtime after delegations.
    return engine.updated_runtime or runtime


async def test_exhausted_budget_still_settles_unresolved_step():
    world, runtime, path, llm = await _setup()
    engine = InvestigationEngine(world.deps)
    llm.add(
        "next_action",
        h.turn_complete_step(["proof-budget"], outcome="blocked", reason="gap stays open"),
    )
    # The next_action decision itself spends the only normal permit, so the
    # completion gate below runs with the normal lane exhausted.
    decided, compiled, _ = await engine.decide(runtime, GraphObservation())
    assert compiled == []
    step = next(s for s in decided.steps if s.step_id == "step-1")
    assert str(step.status) == "blocked"
    assert engine.planning_diagnostic is None
    assert _settled_runtime(engine, runtime).unresolved_evidence_ids != []
    assert "completion_review" not in _routes(llm)


async def test_exhausted_budget_still_blocks_success_step_review():
    world, runtime, path, llm = await _setup()
    engine = InvestigationEngine(world.deps)
    llm.add("next_action", h.turn_complete_step(["proof-budget"]))
    decided, compiled, outcome = await engine.decide(runtime, GraphObservation())
    assert compiled == []
    assert outcome == "continue_planning"
    assert engine.planning_diagnostic is not None
    step = next(s for s in decided.steps if s.step_id == "step-1")
    assert str(step.status) != "completed"
    assert "completion_review" not in _routes(llm)


async def test_exhausted_budget_still_settles_unresolved_task():
    world, runtime, path, llm = await _setup()
    engine = InvestigationEngine(world.deps)
    llm.add(
        "next_action",
        {
            "mode": "complete_task",
            "reason": "task gap stays open",
            "actions": [],
            "path": None,
            "path_step_id": None,
            "completion": {
                "evidence_ids": ["proof-budget"],
                "outcome": "blocked",
                "reason": "task gap stays open",
                "criterion_results": [],
            },
            "evidence_request": {},
            "hypothesis_update": {},
        },
    )
    _, compiled, outcome = await engine.decide(runtime, GraphObservation())
    assert compiled == []
    assert outcome == "task_blocked"
    assert "task_completion_review" not in _routes(llm)
    assert (await world.tasks.get_task(runtime.active_task_id)).status is TaskStatus.BLOCKED


async def test_total_exhaustion_still_settles_unresolved_step():
    world, runtime, path, llm = await _setup()
    engine = InvestigationEngine(world.deps)
    # No lane left at all: a single total permit spent by next_action.
    engine._decision_budget = lambda: DecisionBudget.reasoning_chain(1, 0)
    llm.add(
        "next_action",
        h.turn_complete_step(["proof-budget"], outcome="inconclusive", reason="gap stays open"),
    )
    decided, compiled, _ = await engine.decide(runtime, GraphObservation())
    assert compiled == []
    step = next(s for s in decided.steps if s.step_id == "step-1")
    assert str(step.status) == "blocked"
    assert _settled_runtime(engine, runtime).unresolved_evidence_ids != []
    assert "completion_review" not in _routes(llm)


async def test_unresolved_false_reference_still_rejected_without_budget():
    world, runtime, path, llm = await _setup()
    engine = InvestigationEngine(world.deps)
    llm.add(
        "next_action",
        h.turn_complete_step(["missing-evidence"], outcome="blocked", reason="gap stays open"),
    )
    # The structured unresolved shape bypasses the gate, but the existing
    # reference check still rejects the false citation; nothing settles.
    import pytest as _pytest

    with _pytest.raises(DecisionFailure):
        await engine.decide(runtime, GraphObservation())
    assert "completion_review" not in _routes(llm)
    step = next(s for s in path.steps if s.step_id == "step-1")
    assert str(step.status) == "pending"
    assert _settled_runtime(engine, runtime).unresolved_evidence_ids == []


async def test_exhausted_budget_still_settles_unresolved_goal():
    world, runtime, path, llm = await _setup(task_status=TaskStatus.COMPLETED)
    engine = InvestigationEngine(world.deps)
    llm.add(
        "next_action",
        {
            "mode": "complete_goal",
            "reason": "mission gap stays open",
            "actions": [],
            "path": None,
            "path_step_id": None,
            "completion": {
                "evidence_ids": ["proof-budget"],
                "outcome": "blocked",
                "reason": "mission gap stays open",
                "criterion_results": [],
            },
            "evidence_request": {},
            "hypothesis_update": {},
        },
    )
    _, compiled, outcome = await engine.decide(runtime, GraphObservation())
    assert compiled == []
    assert outcome == "investigation_finished"
    assert engine.planning_diagnostic is None
    assert "goal_completion_review" not in _routes(llm)
    assert (await world.tasks.get_mission(runtime.mission_id)).status is MissionStatus.BLOCKED
