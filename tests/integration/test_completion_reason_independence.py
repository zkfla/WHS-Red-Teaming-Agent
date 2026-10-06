"""Equivalent reasons cannot bypass evidence or add expression-specific gates."""

from redteam.orchestration.completion import validate_completion
from redteam.orchestration.paths import CompletionCriterion, PathPlan, PathStep
from tests.integration import _engine_llm_harness as h


async def test_empty_evidence_is_rejected_equally_for_different_reasons():
    seed = await h.seed_host("run-expression")
    world = h.make_world(h.ScriptedLLM(), graph_store=seed.store)
    step = PathStep(
        step_id="step", objective="observe response", target_node_ids=[seed.node_id],
        completion_criteria=[CompletionCriterion(kind="evidence", description="response")],
    )
    path = PathPlan(
        path_id="path-expression", run_id="run-expression", version=1, status="active",
        reason="investigation", goal="observe response", steps=[step],
    )
    await world.paths.register(path)
    outcomes = []
    for reason in ("no candidates left", "후보가 없다", "investigation complete"):
        issues = await validate_completion(
            path=path, step=step,
            proposal={"outcome": "success", "reason": reason, "evidence_ids": []},
            evidence_store=world.evidence, graph_service=seed.service,
            run_id="run-expression", path_store=world.paths,
        )
        assert issues
        assert any("completion needs real evidence" in issue for issue in issues)
        outcomes.append(list(issues))
    assert outcomes[0] == outcomes[1] == outcomes[2]
