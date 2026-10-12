"""Engine LLM-flow integration tests (scripted model, no network, no clock).

Every test drives production engine/verification/completion code with a
scripted stand-in model from ._engine_llm_harness. Nothing here runs the
suite itself; AGY reviews before anything executes.
"""

from __future__ import annotations

import pytest

from redteam.core.enums import Verdict
from redteam.orchestration.analysis import StructuredLLMAnalysisVerifier, VerificationRequest
from redteam.orchestration.cognition import DecisionFailure, StructuredDecisionClient
from redteam.orchestration.completion import validate_completion
from redteam.orchestration.engine import InvestigationEngine
from redteam.orchestration.expectations import Expectation
from redteam.orchestration.paths import CompletionCriterion, PathPlan, PathStep
from tests.integration import _engine_llm_harness as h


async def test_prior_version_evidence_requires_unchanged_step_and_ancestors():
    llm = h.ScriptedLLM()
    seed = await h.seed_host("run-lineage")
    world = h.make_world(llm, graph_store=seed.store)
    root = PathStep(
        step_id="root",
        objective="collect prerequisite",
        status="completed",
        target_node_ids=[seed.node_id],
        allowed_operations=[h.CANONICAL_OP],
    )
    child = PathStep(
        step_id="child",
        objective="collect evidence",
        depends_on=["root"],
        target_node_ids=[seed.node_id],
        allowed_operations=[h.CANONICAL_OP],
        completion_criteria=[
            CompletionCriterion(kind="evidence", description="result"),
        ],
    )
    first = PathPlan(
        path_id="lineage",
        run_id="run-lineage",
        version=1,
        status="active",
        reason="initial",
        goal="goal",
        steps=[root, child],
    )
    await world.paths.register(first)
    evidence = h.make_tool_evidence(
        evidence_id="ev-lineage",
        run_id="run-lineage",
        engagement_id="eng-t1",
        action_id="action-lineage",
        invocation_id="inv-lineage",
        path_id="lineage",
        path_version=1,
        step_id="child",
        target_node_id=seed.node_id,
    )
    await world.evidence.append(evidence)
    second = first.model_copy(deep=True, update={"version": 2, "parent_version": 1})
    second = await world.paths.register(second, expected_version=1)
    proposal = {"evidence_ids": ["ev-lineage"], "outcome": "success", "reason": "result"}
    args = dict(
        proposal=proposal,
        evidence_store=world.evidence,
        graph_service=seed.service,
        run_id="run-lineage",
        path_store=world.paths,
    )
    assert await validate_completion(path=second, step=second.steps[1], **args) == []
    third = second.model_copy(
        deep=True, update={"version": 3, "parent_version": 2, "changed_step_ids": ["root"]}
    )
    third.steps[0].objective = "different prerequisite"
    third = await world.paths.register(third, expected_version=2)
    await world.paths.update_step("run-lineage", "lineage", 3, "root", "completed")
    third = await world.paths.get_active("run-lineage")
    assert third is not None
    issues = await validate_completion(path=third, step=third.steps[1], **args)
    assert any("lineage" in item or "changed" in item or "redefined" in item for item in issues)








async def test_schema_violation_raises_without_empty_result():
    """Off-schema answers spend the budget and raise, never return {}."""
    client = StructuredDecisionClient(h.AlwaysInvalidLLM(), max_attempts=2)
    schema = {
        "type": "object",
        "properties": {"ok": {"type": "boolean"}},
        "required": ["ok"],
        "additionalProperties": False,
    }
    with pytest.raises(DecisionFailure) as caught:
        await client.ask(stage="t1-schema", payload={"a": 1}, response_schema=schema)
    assert caught.value.kind == "budget"
    assert caught.value.attempts == 2
    assert caught.value.stage == "t1-schema"


async def test_meaning_and_receipt_gates_block_completion():
    """A claimed success without grounding (or without a FINAL receipt) holds."""
    llm = h.ScriptedLLM()
    seed = await h.seed_host("run-t1-sem")
    world = h.make_world(llm, graph_store=seed.store)

    hid = "hyp-t1-sem"
    await h.record_open_hypothesis(seed.service, hid, seed.node_id, "run-t1-sem")
    evidence = h.make_tool_evidence(
        evidence_id="ev-t1-sem",
        run_id="run-t1-sem",
        engagement_id="eng-t1",
        action_id="action-t1-sem",
        invocation_id="inv-t1-sem",
        path_id="path-t1-sem",
        path_version=1,
        step_id="step-1",
        target_node_id=seed.node_id,
        hypothesis_id=hid,
        output={"note": "nothing found"},
    )
    await world.evidence.append(evidence)
    proposal, result = h.make_proposal_result(
        run_id="run-t1-sem",
        engagement_id="eng-t1",
        action_id="action-t1-sem",
        invocation_id="inv-t1-sem",
        hypothesis_id=hid,
        output={"note": "nothing found"},
    )
    # The model claims success but the recorded expectation does not hold.
    expectation = Expectation(
        kind="output_contains",
        value="admin-token-xyz",
        description="admin token visible",
    )
    verifier = StructuredLLMAnalysisVerifier(llm, world.evidence, seed.service)
    llm.add("analysis_verify", h.meaning("success", True))
    assessment = await verifier.verify(
        VerificationRequest(
            proposal=proposal,
            result=result,
            expectation=expectation,
            source_refs=["ev-t1-sem"],
            hypothesis_claim="host exposes an admin token",
        )
    )
    assert assessment.verdict is Verdict.UNKNOWN
    assert "expectation" in assessment.reason

    def path_with(kind: str, hid_: str | None = None) -> PathPlan:
        criterion = CompletionCriterion(
            kind=kind, description="x", min_evidence=1, hypothesis_id=hid_
        )
        return PathPlan(
            path_id="path-t1-sem",
            run_id="run-t1-sem",
            version=1,
            status="active",
            reason="t1",
            goal="g",
            steps=[
                PathStep(
                    step_id="step-1",
                    objective="x",
                    agent_id=h.RECON_AGENT,
                    target_node_ids=[seed.node_id],
                    allowed_operations=[h.CANONICAL_OP],
                    completion_criteria=[criterion],
                )
            ],
        )

    step = path_with("hypothesis_confirmed", hid).steps[0]
    issues = await validate_completion(
        path=path_with("hypothesis_confirmed", hid),
        step=step,
        proposal={
            "evidence_ids": ["ev-t1-sem"],
            "outcome": "success",
            "reason": "model claims success",
            "criterion_results": [],
        },
        evidence_store=world.evidence,
        graph_service=seed.service,
        run_id="run-t1-sem",
    )
    assert issues, "open hypothesis plus interim-only receipt must not complete"
    # An open hypothesis blocks before the receipt gate, so confirm it here:
    # the stale-receipt assertions below must reach the receipt check itself.
    await seed.service.resolve_hypothesis(hid, status="confirmed", step=0)
    issues = await validate_completion(
        path=path_with("hypothesis_confirmed", hid),
        step=step,
        proposal={
            "evidence_ids": ["ev-t1-sem"],
            "outcome": "success",
            "reason": "model claims success",
            "criterion_results": [],
        },
        evidence_store=world.evidence,
        graph_service=seed.service,
        run_id="run-t1-sem",
    )
    assert any("final admission" in i for i in issues)
    # The interim receipt verify() wrote changes nothing; the evidence stays.
    assert await world.evidence.get("ev-t1-sem") is not None

    # A FINAL receipt for a stale action/invocation pair still does not count.
    stale_proposal, stale_result = h.make_proposal_result(
        run_id="run-t1-sem",
        engagement_id="eng-t1",
        action_id="action-t1-stale",
        invocation_id="inv-t1-stale",
        hypothesis_id=hid,
    )
    await h.admit_final(
        world.evidence,
        proposal=stale_proposal,
        result=stale_result,
        outcome="success",
        verdict=Verdict.PASS,
        claim_supported=True,
        source_refs=["ev-t1-sem"],
    )
    issues = await validate_completion(
        path=path_with("hypothesis_confirmed", hid),
        step=step,
        proposal={
            "evidence_ids": ["ev-t1-sem"],
            "outcome": "success",
            "reason": "model claims success",
            "criterion_results": [],
        },
        evidence_store=world.evidence,
        graph_service=seed.service,
        run_id="run-t1-sem",
    )
    assert any("final admission" in i for i in issues)
    assert await world.evidence.get("ev-t1-sem") is not None

    # A matching FINAL success/violation receipt plus a verified finding lands poc.
    poc_evidence = h.make_tool_evidence(
        evidence_id="ev-t1-poc",
        run_id="run-t1-sem",
        engagement_id="eng-t1",
        action_id="action-t1-poc",
        invocation_id="inv-t1-poc",
        path_id="path-t1-sem",
        path_version=1,
        step_id="step-1",
        target_node_id=seed.node_id,
    )
    await world.evidence.append(poc_evidence)
    poc_proposal, poc_result = h.make_proposal_result(
        run_id="run-t1-sem",
        engagement_id="eng-t1",
        action_id="action-t1-poc",
        invocation_id="inv-t1-poc",
    )
    await h.admit_final(
        world.evidence,
        proposal=poc_proposal,
        result=poc_result,
        outcome="success",
        verdict=Verdict.VIOLATION,
        claim_supported=True,
        source_refs=["ev-t1-poc"],
    )
    await h.record_verified_finding(
        seed.service,
        finding_id="finding-t1-poc",
        evidence_ids=["ev-t1-poc"],
        target_node_id=seed.node_id,
    )
    poc_step = path_with("poc").steps[0]
    issues = await validate_completion(
        path=path_with("poc"),
        step=poc_step,
        proposal={
            "evidence_ids": ["ev-t1-poc"],
            "outcome": "success",
            "reason": "poc backed",
            "criterion_results": [],
        },
        evidence_store=world.evidence,
        graph_service=seed.service,
        run_id="run-t1-sem",
    )
    assert issues == []


def test_production_entrypoint_refuses_fake_model():
    """No fake provider reaches the production model builder; no rule fallback."""
    from redteam.app.bootstrap import build_llm_client
    from redteam.core.config import LLMConfig

    with pytest.raises(ValueError, match="production requires a real LLM provider"):
        build_llm_client(LLMConfig(provider="fake", default_model="x"))
    with pytest.raises(ValueError, match="production requires a real LLM provider"):
        build_llm_client(LLMConfig(provider="openai", default_model="x"))

    llm = h.ScriptedLLM()
    world = h.make_world(llm)
    engine = InvestigationEngine(world.deps)
    assert engine.deps.llm_client is llm
    assert isinstance(engine.client, StructuredDecisionClient)
