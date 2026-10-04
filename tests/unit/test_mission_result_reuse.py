"""A proved goal verdict may be reused only for the meaning originally reviewed."""

from redteam.core.contracts.mission import MissionCriterionResult
from redteam.core.enums import TaskStatus
from redteam.orchestration.cognition import DecisionBudget
from redteam.orchestration.paths import PathPlan
from redteam.orchestration.replanning import _terminal_criterion_results
from redteam.orchestration.result_claims import mission_result_purpose
from tests.integration import _engine_llm_harness as h


async def test_same_criteria_text_does_not_reuse_verdict_after_goal_change():
    seed = await h.seed_host("run-purpose-reuse")
    world = h.make_world(h.ScriptedLLM(), graph_store=seed.store)
    mission, task = h.make_mission_task(
        mission_id="mission-purpose",
        engagement_id="eng-t1",
        criteria=["response collected"],
    )
    task = task.model_copy(update={"status": TaskStatus.COMPLETED})
    raw = h.draft_path(seed.node_id)
    raw.update(
        path_id="purpose-path",
        run_id="run-purpose-reuse",
        version=1,
        status="active",
        goal=task.objective,
        mission_id=str(mission.mission_id),
        mission_task_id=str(task.task_id),
    )
    await world.paths.register(PathPlan.model_validate(raw))
    evidence = h.make_tool_evidence(
        evidence_id="purpose-proof",
        run_id="run-purpose-reuse",
        engagement_id="eng-t1",
        action_id="purpose-action",
        invocation_id="purpose-invocation",
        path_id="purpose-path",
        path_version=1,
        step_id="step-1",
        target_node_id=seed.node_id,
    )
    await world.evidence.append(evidence)
    claim = {
        "kind": "semantic",
        "description": "inventory response collected",
        "evidence_ids": ["purpose-proof"],
        "target_node_ids": [seed.node_id],
    }
    prior = MissionCriterionResult(
        criterion="response collected",
        met=True,
        task_ids=[task.task_id],
        evidence_ids=["purpose-proof"],
        result_claims=[claim],
        purpose_hash=mission_result_purpose(mission),
    )
    mission = mission.model_copy(update={"criterion_results": [prior]})
    kwargs = dict(
        receipt_bound={str(task.task_id): {"purpose-proof"}},
        owners={"purpose-proof": str(task.task_id)},
        evidence_store=world.evidence,
        path_store=world.paths,
        run_id="run-purpose-reuse",
    )
    unchanged, _ = await _terminal_criterion_results(
        mission,
        [task],
        [evidence],
        None,
        DecisionBudget(1),
        **kwargs,
    )
    assert unchanged[0].met
    for change in (
        {"objective": "prove account isolation"},
        {"operator_constraints": {"required_account": "reviewer"}},
    ):
        current = mission.model_copy(update=change)
        stale, _ = await _terminal_criterion_results(
            current,
            [task],
            [evidence],
            None,
            DecisionBudget(1),
            **kwargs,
        )
        assert not stale[0].met
    assert (await world.evidence.get("purpose-proof")).metadata["tool_result"] == evidence.metadata[
        "tool_result"
    ]
