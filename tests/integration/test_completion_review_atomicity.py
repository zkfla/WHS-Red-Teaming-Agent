"""Actual completion reviews may not commit after their inputs have changed."""

from __future__ import annotations

import pytest

from redteam.attack_graph.observation import GraphObservation
from redteam.core.enums import MissionStatus, TaskStatus
from redteam.core.ids import TaskId
from redteam.orchestration.coordination import StateConflict, coordinator_for
from redteam.orchestration.engine import InvestigationEngine, TurnDecision
from redteam.orchestration.operator_intents import (
    InMemoryOperatorIntentStore,
    OperatorIntentService,
)
from redteam.orchestration.paths import PathPlan
from tests.integration import _engine_llm_harness as h


class Reviewer:
    def __init__(self):
        self.hook = None
        self.criteria = []
        self.calls = 0

    async def complete_structured(self, **kwargs):
        self.calls += 1
        if self.hook:
            await self.hook()
        props = kwargs["response_schema"].get("properties", {})
        if "criteria" in props:
            return h.goal_accept(self.criteria)
        if "claims" in props:
            import json as _json
            try:
                _payload = _json.loads(kwargs["messages"][0]["content"])
                _excerpts = _payload.get("evidence_excerpts", [])
                _ids = [
                    str(e.get("evidence_id"))
                    for e in _excerpts
                    if isinstance(e, dict)
                    and str(e.get("evidence_id", "")).strip()
                ]
            except Exception:
                _ids = []
            if not _ids:
                _ids = ["proof-race"]
            return h.task_accept(_ids)
        return h.completion_accept()


async def setup(mode, reviewer=None):
    reviewer = reviewer if reviewer is not None else Reviewer()
    seed = await h.seed_host("run-review-race")
    world = h.make_world(reviewer, graph_store=seed.store)
    mission, task = h.make_mission_task(
        mission_id="mission-race", engagement_id="eng-t1", criteria=["actual response"]
    )
    mission.status = MissionStatus.ACTIVE
    # Goal settlement needs completed tasks to reach the review; step/task keep running.
    task.status = TaskStatus.COMPLETED if mode == "goal" else TaskStatus.RUNNING
    await world.tasks.create_mission(mission)
    await world.tasks.create_task(task)
    runtime = h.make_runtime(
        "run-review-race", "eng-t1", mission_id="mission-race", task_id=str(task.task_id)
    )
    await coordinator_for(world.tasks, world.paths).bind(
        str(runtime.run_id), str(mission.mission_id), str(mission.engagement_id)
    )
    raw = h.draft_path(seed.node_id)
    # Storage fixture: hydrate code-owned runtime fields onto minimal draft.
    raw.update(
        path_id="path-race",
        run_id=str(runtime.run_id),
        version=1,
        status="active",
        mission_id=str(mission.mission_id),
        mission_task_id=str(task.task_id),
        goal=task.objective,
    )
    if mode != "step":
        raw["steps"][0].update(status="completed", outcome="success", evidence_ids=["proof-race"])
    path = await world.paths.register(PathPlan.model_validate(raw))
    await world.evidence.append(
        h.make_tool_evidence(
            evidence_id="proof-race",
            run_id=str(runtime.run_id),
            engagement_id="eng-t1",
            action_id="action-race",
            invocation_id="inv-race",
            path_id=path.path_id,
            path_version=path.version,
            step_id="step-1",
            target_node_id=seed.node_id,
        )
    )
    reviewer.criteria = [
        {
            "criterion": "actual response",
            "met": True,
            "evidence_ids": ["proof-race"],
            "claims": [
                {
                    "description": "actual response observed in stored evidence",
                    "kind": "semantic",
                    "evidence_ids": ["proof-race"],
                }
            ],
        }
    ]
    return world, runtime, path, reviewer


@pytest.mark.parametrize("mode", ["step", "task", "goal"])
@pytest.mark.parametrize("change", ["task", "progress", "intent"])
async def test_review_rejects_changed_tasks_progress_or_pending_instructions(mode, change):
    world, runtime, path, reviewer = await setup(mode)

    async def mutate():
        if change == "task":
            await world.tasks.update_priority(runtime.active_task_id, 321, now=h.utcnow())
        elif change == "progress":
            await world.paths.update_step(
                str(runtime.run_id),
                path.path_id,
                path.version,
                "step-1",
                "blocked",
                outcome="blocked",
            )
        else:
            service = OperatorIntentService(
                store=InMemoryOperatorIntentStore(),
                task_graph_store=world.tasks,
                path_store=world.paths,
            )
            received = await service.receive(
                "exclude mobile", mission_id=runtime.mission_id, run_id=runtime.run_id
            )
            assert received.intent_id is not None

    reviewer.hook = mutate
    engine = InvestigationEngine(world.deps)
    context = await engine.context(runtime, GraphObservation())
    with pytest.raises(StateConflict):
        if mode == "step":
            await engine.complete_step(
                path,
                TurnDecision.model_validate(h.turn_complete_step(["proof-race"])),
                runtime,
                context,
            )
        elif mode == "task":
            await engine.complete_task(
                path,
                TurnDecision(mode="complete_task", reason="evaluate actual evidence"),
                runtime,
                context,
            )
        else:
            await engine.complete_goal(
                path, TurnDecision.model_validate(h.turn_complete_goal()), runtime, context
            )
    assert reviewer.calls == 1
    # Goal setup uses a completed task so the review is reachable; step/task stay running.
    expected_task = TaskStatus.COMPLETED if mode == "goal" else TaskStatus.RUNNING
    assert (await world.tasks.get_task(runtime.active_task_id)).status is expected_task
    assert (await world.tasks.get_mission(runtime.mission_id)).status is MissionStatus.ACTIVE


class _ScopeCapture:
    """Record review requests; answer the step review from a script."""

    def __init__(self, *, accepted: bool, reason: str):
        self.calls = 0
        self.systems: list[str] = []
        self.contents: list[str] = []
        self._answer = {"accepted": accepted, "reason": reason}

    async def complete_structured(self, **kwargs):
        self.calls += 1
        self.systems.append(kwargs.get("system") or "")
        self.contents.append(str(kwargs.get("messages") or ""))
        return dict(self._answer)

    async def complete(self, **kwargs):
        return ""


async def test_step_review_request_carries_step_scope_only():
    reviewer = _ScopeCapture(accepted=True, reason="step objective met on stored evidence")
    world, runtime, path, _ = await setup("step", reviewer=reviewer)
    engine = InvestigationEngine(world.deps)
    context = await engine.context(runtime, GraphObservation())

    finished, errors = await engine.complete_step(
        path,
        TurnDecision.model_validate(h.turn_complete_step(["proof-race"])),
        runtime,
        context,
    )

    assert errors == []
    assert reviewer.calls == 1
    system = reviewer.systems[0]
    assert "supplied path step only" in system
    assert "running mission task" in system
    content = reviewer.contents[0]
    assert "confirm the host answers inventory" in content
    assert "harness completion condition" in content
    # Stage-only progress: the step completes while its task still runs.
    step = next(s for s in finished.steps if s.step_id == "step-1")
    assert str(step.status) == "completed"
    assert (await world.tasks.get_task(runtime.active_task_id)).status is TaskStatus.RUNNING
    assert (await world.tasks.get_mission(runtime.mission_id)).status is MissionStatus.ACTIVE


async def test_step_review_rejection_returns_the_actual_reason():
    marker = "inventory shows the port closed, no banner captured"
    reviewer = _ScopeCapture(accepted=False, reason=marker)
    world, runtime, path, _ = await setup("step", reviewer=reviewer)
    engine = InvestigationEngine(world.deps)
    context = await engine.context(runtime, GraphObservation())

    _, errors = await engine.complete_step(
        path,
        TurnDecision.model_validate(h.turn_complete_step(["proof-race"])),
        runtime,
        context,
    )

    assert reviewer.calls == 1
    assert len(errors) == 1
    assert marker in errors[0]
    assert "semantic completion review rejected" in errors[0]
    # The next decision consumes the actual reason through safe feedback.
    from redteam.orchestration.cognition import DecisionBudget

    assert marker in " ".join(engine._safe_errors(errors, DecisionBudget(3)))


async def test_step_review_without_evidence_keeps_the_guard():
    reviewer = _ScopeCapture(accepted=True, reason="must not be reached")
    world, runtime, path, _ = await setup("step", reviewer=reviewer)
    engine = InvestigationEngine(world.deps)
    context = await engine.context(runtime, GraphObservation())

    _, errors = await engine.complete_step(
        path,
        TurnDecision.model_validate(h.turn_complete_step([])),
        runtime,
        context,
    )

    assert errors != []
    assert reviewer.calls == 0


async def test_each_goal_criterion_records_only_its_actual_evidence_owner():
    world, runtime, path, reviewer = await setup("goal")
    mission = await world.tasks.get_mission(runtime.mission_id)
    await world.tasks.update_mission(
        mission.model_copy(
            update={
                "success_criteria": ["first response", "second response"],
                "revision": mission.revision + 1,
            }
        )
    )
    task = await world.tasks.get_task(runtime.active_task_id)
    first = task.model_copy(
        update={"task_id": TaskId("task-first"), "status": TaskStatus.COMPLETED}
    )
    await world.tasks.create_task(first)
    # A historical accepted path retains the first task's own measured receipt.
    first_path = path.model_copy(
        deep=True,
        update={
            "path_id": "path-first",
            "status": "candidate",
            "mission_task_id": str(first.task_id),
        },
    )
    first_path.steps[0].evidence_ids = ["proof-first"]
    # Register before restoring the existing active path, so the stored snapshot
    # is accepted while its evidence ownership remains immutable.
    first_path.status = "active"
    await world.paths.register(first_path, expected_version=1, expected_path_id=path.path_id)
    current = path.model_copy(deep=True, update={"version": 2, "parent_version": 1})
    current = await world.paths.register(
        current, expected_version=1, expected_path_id=first_path.path_id
    )
    await world.evidence.append(
        h.make_tool_evidence(
            evidence_id="proof-first",
            run_id=str(runtime.run_id),
            engagement_id="eng-t1",
            action_id="action-first",
            invocation_id="inv-first",
            path_id=first_path.path_id,
            path_version=1,
            step_id="step-1",
            target_node_id=path.steps[0].target_node_ids[0],
        )
    )
    reviewer.criteria = [
        {
            "criterion": "first response",
            "met": True,
            "evidence_ids": ["proof-first"],
            "claims": [
                {
                    "description": "actual first response observed in stored evidence",
                    "kind": "semantic",
                    "evidence_ids": ["proof-first"],
                }
            ],
        },
        {
            "criterion": "second response",
            "met": True,
            "evidence_ids": ["proof-race"],
            "claims": [
                {
                    "description": "actual second response observed in stored evidence",
                    "kind": "semantic",
                    "evidence_ids": ["proof-race"],
                }
            ],
        },
    ]
    engine = InvestigationEngine(world.deps)
    errors = await engine.complete_goal(
        current,
        TurnDecision.model_validate(h.turn_complete_goal()),
        runtime,
        await engine.context(runtime, GraphObservation()),
    )
    assert errors == []
    final = await world.tasks.get_mission(runtime.mission_id)
    assert final.status is MissionStatus.COMPLETED
    results = {item.criterion: item for item in final.criterion_results}
    assert results["first response"].task_ids == [first.task_id]
    assert results["second response"].task_ids == [task.task_id]
