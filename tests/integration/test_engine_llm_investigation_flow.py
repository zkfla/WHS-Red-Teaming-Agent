"""Engine LLM-flow integration tests (scripted model, no network, no clock).

Every test drives production engine/verification/completion code with a
scripted stand-in model from ._engine_llm_harness. Nothing here runs the
suite itself; AGY reviews before anything executes.
"""

from __future__ import annotations

import pytest

from redteam.attack_graph.observation import GraphObservation
from redteam.core.enums import MissionStatus, TaskStatus, Verdict
from redteam.core.ids import AgentId, MissionId, TaskId
from redteam.orchestration.action_naming import CANONICAL_OPERATIONS
from redteam.orchestration.agents.providers import PlaybookCandidateProvider
from redteam.orchestration.analysis import StructuredLLMAnalysisVerifier, VerificationRequest
from redteam.orchestration.cognition import DecisionFailure, StructuredDecisionClient
from redteam.orchestration.completion import validate_completion
from redteam.orchestration.engine import ActionIntent, InvestigationEngine, TurnDecision
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


async def test_llm_draft_review_action_to_step_completion():
    """Draft -> review -> one compiled action -> step completion, all LLM-led."""
    llm = h.ScriptedLLM()
    seed = await h.seed_host("run-t1-flow")
    world = h.make_world(llm, graph_store=seed.store)
    runtime = h.make_runtime("run-t1-flow", "eng-t1")
    engine = InvestigationEngine(world.deps)

    llm.add("path_draft", h.draft_path(seed.node_id))
    llm.add("path_review", h.review_accept())
    llm.add("next_action", h.turn_actions(seed.node_id))
    path, compiled, note = await engine.decide(runtime, GraphObservation())

    assert note is None
    assert len(compiled) == 1
    # The playbook only knows recon.probe/http_probe/ping: an inventory
    # tool in the compiled slot proves the model intent ran, not a rule.
    assert str(compiled[0].tool.tool_id) == h.INVENTORY_TOOL
    # Direct-tool binding records the selected tool id, not the legacy
    # capability.operation key: the ledger key must name the compiled tool.
    assert str(compiled[0].plan_step.operation_key) == str(compiled[0].tool.tool_id)
    active = await world.paths.get_active("run-t1-flow")
    assert active is not None
    assert active.review_records.get("decision") == "accept"

    # One allowed call answers; its stored result becomes the evidence.
    evidence = h.make_tool_evidence(
        evidence_id="ev-t1-1",
        run_id="run-t1-flow",
        engagement_id="eng-t1",
        action_id="action-t1-1",
        invocation_id="inv-t1-1",
        path_id=path.path_id,
        path_version=path.version,
        step_id="step-1",
        target_node_id=seed.node_id,
    )
    await world.evidence.append(evidence)
    llm.add("completion_review", h.completion_accept())
    decision = TurnDecision.model_validate(h.turn_complete_step(["ev-t1-1"]))
    context = await engine.context(runtime, GraphObservation())
    finished, errors = await engine.complete_step(path, decision, runtime, context)

    assert errors == []
    step = next(s for s in finished.steps if s.step_id == "step-1")
    assert str(step.status) == "completed"
    assert await world.evidence.get("ev-t1-1") is not None


async def test_successful_step_completion_does_not_invent_a_rejection(monkeypatch):
    from redteam.orchestration.cognition import DecisionBudget

    llm = h.ScriptedLLM()
    seed = await h.seed_host("run-completion-diagnostic")
    world = h.make_world(llm, graph_store=seed.store)
    runtime = h.make_runtime("run-completion-diagnostic", "eng-t1")
    engine = InvestigationEngine(world.deps)
    llm.add("path_draft", h.draft_path(seed.node_id))
    llm.add("path_review", h.review_accept())
    llm.add("next_action", h.turn_actions(seed.node_id))
    path, _, _ = await engine.decide(runtime, GraphObservation())
    await world.evidence.append(h.make_tool_evidence(
        evidence_id="ev-completion-diagnostic", run_id=str(runtime.run_id),
        engagement_id="eng-t1", action_id="action-completion-diagnostic",
        invocation_id="inv-completion-diagnostic", path_id=path.path_id,
        path_version=path.version, step_id="step-1", target_node_id=seed.node_id,
    ))
    budget = DecisionBudget.reasoning_chain(8, 3)
    monkeypatch.setattr(engine, "_decision_budget", lambda: budget)
    llm.add("next_action", h.turn_complete_step(["ev-completion-diagnostic"]))
    llm.add("completion_review", h.completion_accept())
    llm.add("next_action", {**h.turn_complete_goal("review finished"), "mode": "hold"})
    finished, _, outcome = await engine.decide(runtime, GraphObservation())
    assert finished.steps[0].status == "completed"
    assert outcome == "review finished"
    assert budget.last_failure is None


async def test_tool_outside_playbook_is_exposed_and_compiles():
    """recon.inventory rides recon.service_discovery although no playbook emits it."""
    assert "service_discovery" in CANONICAL_OPERATIONS
    llm = h.ScriptedLLM()
    seed = await h.seed_host("run-t1-newtool")
    world = h.make_world(llm, graph_store=seed.store)
    runtime = h.make_runtime("run-t1-newtool", "eng-t1")
    engine = InvestigationEngine(world.deps)

    spec = world.deps.agents.get(AgentId(h.RECON_AGENT))
    assert spec is not None
    drafts = await PlaybookCandidateProvider().propose(
        spec, seed.service, GraphObservation(), step=0
    )
    assert h.INVENTORY_TOOL not in {str(d.plan_step.tool_id) for d in drafts}

    context = await engine.context(runtime, GraphObservation())
    assert h.INVENTORY_TOOL in {t["tool_id"] for t in context["tools"]}
    step = PathStep(
        step_id="step-1",
        objective="inventory the host",
        agent_id=h.RECON_AGENT,
        target_node_ids=[seed.node_id],
        allowed_operations=[h.CANONICAL_OP],
        completion_criteria=[
            CompletionCriterion(kind="evidence", description="x", min_evidence=1),
        ],
    )
    path = PathPlan(
        path_id="path-t1-newtool",
        run_id="run-t1-newtool",
        version=1,
        status="active",
        reason="t1",
        goal="g",
        steps=[step],
    )
    await world.paths.register(path)
    intent = ActionIntent(
        path_step_id="step-1",
        agent_id=h.RECON_AGENT,
        target_node_id=seed.node_id,
        capability="recon",
        operation="service_discovery",
        tool_id=h.INVENTORY_TOOL,
        arguments={},
        reason="model chose the new tool",
    )
    compiled, errors = await engine.compile_actions(path, [intent], runtime, context)
    assert errors == []
    assert len(compiled) == 1
    assert str(compiled[0].tool.tool_id) == h.INVENTORY_TOOL


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


async def test_budget_zero_allows_completion_review_but_blocks_new_execution():
    """One allowed call settles the goal: review still passes at budget 0."""
    from redteam.event_bus.memory import InMemoryEventBus
    from redteam.orchestration.nodes.plan import plan_node
    from redteam.orchestration.state import TerminationReason, initial_graph_state

    llm = h.ScriptedLLM()
    seed = await h.seed_host("run-t1-zero")
    world = h.make_world(llm, graph_store=seed.store)
    world.deps.event_bus = InMemoryEventBus()
    mission, task = h.make_mission_task(
        mission_id="mission-t1-zero",
        engagement_id="eng-t1",
        criteria=["criterion-1"],
        task_id="task-t1-zero",
    )
    mission = mission.model_copy(update={"status": MissionStatus.ACTIVE})
    await world.tasks.create_mission(mission)
    await world.tasks.create_task(task)
    runtime = h.make_runtime(
        "run-t1-zero",
        "eng-t1",
        mission_id="mission-t1-zero",
        task_id="task-t1-zero",
    )
    runtime.remaining_budget = 0
    from redteam.orchestration.coordination import coordinator_for

    await coordinator_for(world.tasks, world.deps.path_store).bind(
        str(runtime.run_id), str(mission.mission_id), str(mission.engagement_id)
    )
    engine = InvestigationEngine(world.deps)

    # The single allowed call already answered; completion review is
    # non-executing, so budget 0 must not stop it.
    llm.add("path_draft", h.draft_path(seed.node_id))
    llm.add("path_review", h.review_accept())
    llm.add("next_action", h.turn_actions(seed.node_id))
    state = initial_graph_state(runtime)
    state["observation"] = GraphObservation()
    planned = await plan_node(state, deps=world.deps)
    assert planned["runtime"].termination_reason == TerminationReason.BUDGET_EXHAUSTED
    assert planned.get("dispatch_items") == []
    assert await world.work.list_items(run_id=runtime.run_id) == []

    path = await world.paths.get_active("run-t1-zero")
    assert path is not None
    evidence = h.make_tool_evidence(
        evidence_id="ev-t1-zero",
        run_id="run-t1-zero",
        engagement_id="eng-t1",
        action_id="action-t1-zero",
        invocation_id="inv-t1-zero",
        path_id=path.path_id,
        path_version=path.version,
        step_id="step-1",
        target_node_id=seed.node_id,
    )
    await world.evidence.append(evidence)
    llm.add("completion_review", h.completion_accept())
    decision = TurnDecision.model_validate(h.turn_complete_step(["ev-t1-zero"]))
    context = await engine.context(runtime, GraphObservation())
    finished, errors = await engine.complete_step(path, decision, runtime, context)
    assert errors == []

    await world.tasks.transition_task(task.task_id, TaskStatus.RUNNING, now=h.utcnow())
    # New contract: close the delegated task before settling the original goal.
    llm.add("completion_review", h.task_accept(["ev-t1-zero"]))
    _task_decision = TurnDecision(mode="complete_task", reason="task evidence ready")
    _task_context = await engine.context(runtime, GraphObservation())
    _task_errors = await engine.complete_task(finished, _task_decision, runtime, _task_context)
    assert _task_errors == []
    llm.add(
        "goal_completion_review",
        h.goal_accept([{"criterion": "criterion-1", "met": True, "evidence_ids": ["ev-t1-zero"]}]),
    )
    context = await engine.context(runtime, GraphObservation())
    active = await world.paths.get_active("run-t1-zero")
    errors = await engine.complete_goal(
        active,
        TurnDecision.model_validate(h.turn_complete_goal()),
        runtime,
        context,
    )
    assert errors == []
    settled = await world.tasks.get_mission(MissionId("mission-t1-zero"))
    assert settled is not None
    assert settled.status == MissionStatus.COMPLETED
    assert await world.tasks.get_task(TaskId("task-t1-zero")) is not None
