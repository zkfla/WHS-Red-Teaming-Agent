"""Generic pre-push regressions; no lab-specific subjects or live model calls."""

import jsonschema
import pytest

from redteam.core.contracts.target import UrlTarget
from redteam.orchestration.diagnostics import diagnostic_for_exception
from redteam.orchestration.paths.writing import writing_schema
from redteam.orchestration.resource_conflicts import execution_conflicts
from redteam.orchestration.specialist_planning import AttackPathProposal, validate_strategy
from tests.integration._engine_llm_harness import inventory_descriptor
from tests.unit.test_specialist_profile import _strategy


@pytest.mark.parametrize("scheme,port", [("http", 80), ("https", 443)])
@pytest.mark.parametrize("host", ["synthetic.invalid", "[::1]"])
def test_equivalent_default_ports_reserve_same_target(scheme, port, host):
    tool = inventory_descriptor()
    first = execution_conflicts(UrlTarget(url=f"{scheme}://{host}/"), {}, tool)
    second = execution_conflicts(
        UrlTarget(url=f"{scheme}://{host}:{port}/other"), {},
        tool.model_copy(update={"server_id": "independent-backend"}),
    )
    assert set(first) & set(second)


def test_distinct_effective_ports_do_not_share_target_reservation():
    tool = inventory_descriptor()
    first = execution_conflicts(UrlTarget(url="http://synthetic.invalid/"), {}, tool)
    second = execution_conflicts(
        UrlTarget(url="http://synthetic.invalid:8080/"), {},
        tool.model_copy(update={"server_id": "independent-backend"}),
    )
    assert not set(first) & set(second)


def test_planner_schema_rejects_missing_criterion_description():
    value = _strategy()
    value["questions"][0]["completion_criteria"] = [{"kind": "observation"}]
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(value, writing_schema(AttackPathProposal))


def test_unknown_strategy_target_has_safe_specific_diagnostic():
    value = _strategy()
    value["questions"][0]["target_node_ids"] = ["secret-invalid-target"]
    with pytest.raises(ValueError) as caught:
        validate_strategy(AttackPathProposal.model_validate(value), {"knowledge": {}})
    diagnostic = diagnostic_for_exception(caught.value, stage="attack_path_proposal")
    assert diagnostic.code == "specialist_strategy_unknown_target"
    assert diagnostic.location == "$.questions.target_node_ids"
    assert "secret-invalid-target" not in diagnostic.model_dump_json()


def test_planner_uses_existing_semantic_writing_default():
    value = _strategy()
    value["questions"][0]["completion_criteria"] = [{"description": "Observe the bounded response"}]
    jsonschema.validate(value, writing_schema(AttackPathProposal))
    parsed = AttackPathProposal.model_validate(value)
    assert parsed.questions[0].completion_criteria[0].kind == "semantic"
    validate_strategy(parsed, {"knowledge": {"known_targets": ["host:test"]}})


async def test_atomizer_repairs_only_its_stage_with_shared_repair_budget():
    from copy import deepcopy

    from redteam.app.agent_profile import load_specialist_profile
    from redteam.orchestration.cognition import DecisionBudget, StructuredDecisionClient
    from redteam.orchestration.paths import PathPlan
    from redteam.orchestration.planning import BudgetedInference, PlanningRequest
    from redteam.orchestration.specialist_planning import SerialSpecialistPlanningProvider
    from tests.unit.test_specialist_profile import PROFILE, SpecialistLLM, _atomized

    llm = SpecialistLLM()
    invalid = deepcopy(_atomized())
    invalid["origins"] = {}
    llm.add("attack_path_proposal", _strategy())
    llm.add("task_atomization", invalid)
    llm.add("task_atomization", _atomized())
    budget = DecisionBudget.reasoning_chain(8, 3)
    provider = SerialSpecialistPlanningProvider(load_specialist_profile(PROFILE).prompts)
    result = await provider.propose(
        PlanningRequest(
            stage="path_draft", payload={"knowledge": {"known_targets": ["host:test"]}},
            response_schema=writing_schema(PathPlan, selected_tool=True),
            inference=BudgetedInference(StructuredDecisionClient(llm), budget),
        )
    )
    assert result["planning_artifacts"]["specialist_decomposition"]["origins"] == {"s1": "q1"}
    assert [call["route"] for call in llm.calls] == [
        "attack_path_proposal", "task_atomization", "task_atomization",
    ]
    assert budget.normal_used == 2 and budget.repair_used == 1 and budget.used == 3
    assert budget.failure_counts["specialist_atomization_origins_mismatch"] == 1


async def test_atomizer_cannot_multiply_or_reset_the_repair_budget():
    from redteam.app.agent_profile import load_specialist_profile
    from redteam.orchestration.cognition import (
        DecisionBudget,
        DecisionFailure,
        StructuredDecisionClient,
    )
    from redteam.orchestration.paths import PathPlan
    from redteam.orchestration.planning import BudgetedInference, PlanningRequest
    from redteam.orchestration.specialist_planning import SerialSpecialistPlanningProvider
    from tests.unit.test_specialist_profile import PROFILE, SpecialistLLM, _atomized

    llm = SpecialistLLM()
    invalid = _atomized()
    invalid["origins"] = {}
    llm.add("attack_path_proposal", _strategy())
    llm.add("task_atomization", invalid)
    llm.add("task_atomization", invalid)
    budget = DecisionBudget.reasoning_chain(8, 1)
    provider = SerialSpecialistPlanningProvider(load_specialist_profile(PROFILE).prompts)
    with pytest.raises(DecisionFailure) as caught:
        await provider.propose(PlanningRequest(
            stage="path_draft", payload={"knowledge": {"known_targets": ["host:test"]}},
            response_schema=writing_schema(PathPlan, selected_tool=True),
            inference=BudgetedInference(StructuredDecisionClient(llm), budget),
        ))
    assert caught.value.diagnostic.code == "decision_repair_budget_exhausted"
    assert caught.value.diagnostic.last_cause.code == "specialist_atomization_origins_mismatch"
    assert budget.normal_used == 2 and budget.repair_used == 1 and budget.used == 3
    assert [call["route"] for call in llm.calls].count("attack_path_proposal") == 1


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
async def test_equivalent_origins_are_exclusive_in_actual_work_reservations(tmp_path, backend):
    from datetime import datetime, timedelta, timezone

    from redteam.orchestration.candidates.models import PriorityScore
    from redteam.orchestration.scheduler import WorkScheduler
    from redteam.orchestration.work.memory import InMemoryWorkStore
    from redteam.orchestration.work.models import WorkItem
    from redteam.orchestration.work.sqlite import SQLiteWorkStore

    store = InMemoryWorkStore() if backend == "memory" else SQLiteWorkStore(tmp_path / "work.db")
    now = datetime.now(timezone.utc)
    for identity, url in [("first", "http://synthetic.invalid/"),
                          ("second", "http://synthetic.invalid:80/other")]:
        await store.add(WorkItem(
            work_id=identity, run_id="run", candidate_key=identity, agent_id="fixture",
            target_node_ids=[url], priority=PriorityScore(total=1),
            created_at=now, updated_at=now,
            conflict_keys=execution_conflicts(
                UrlTarget(url=url), {}, inventory_descriptor().model_copy(
                    update={"server_id": identity}
                ),
            ),
        ))
    leases = await WorkScheduler(store).lease_batch(
        run_id="run", owner="worker", limit=2, ttl=timedelta(seconds=60), now=now,
    )
    assert len(leases) == 1
    await store.release(leases[0].work_id, owner="worker", now=now)
    assert await store.acquire("second", owner="other", now=now, ttl=timedelta(seconds=60))


async def test_p1_compiler_refuses_different_atomic_subject_without_execution(tmp_path):
    from dataclasses import replace

    from redteam.app.agent_profile import load_specialist_profile
    from redteam.orchestration.engine import ActionIntent, InvestigationEngine
    from redteam.orchestration.paths import PathPlan
    from redteam.orchestration.seed import seed_from_engagement
    from redteam.orchestration.specialist_planning import SerialSpecialistPlanningProvider
    from tests.integration import _engine_llm_harness as h
    from tests.unit.test_specialist_profile import WEB_ROLE, SpecialistLLM, _application, _atomized

    llm = SpecialistLLM()
    world, eng, deps, gateway = await _application(tmp_path, llm)
    # A different known object; merely being known does not assign it to this purpose.
    world.engagement.scope.allowed_hosts.append("independent.invalid")
    await seed_from_engagement(
        world.service, world.engagement, run_id="run-specialist", step=0,
    )
    provider = SerialSpecialistPlanningProvider(
        load_specialist_profile("config/agents/specialists-p1.yaml").prompts,
        enable_replanning=True,
    )
    deps = replace(deps, planning_provider=provider)
    runtime = h.make_runtime("run-specialist", str(eng.engagement_id))
    path = PathPlan.model_validate({
        **_atomized(world.node_id)["path"], "path_id": "path", "version": 1,
        "run_id": str(runtime.run_id), "status": "active", "goal": "Observe assigned subject",
    })
    await deps.path_store.register(path)
    intent = ActionIntent(
        path_step_id="s1", agent_id=WEB_ROLE, target_node_id="host:independent.invalid",
        tool_id=str(inventory_descriptor().tool_id), arguments={"target_ref": "known"},
        reason="Fixture mismatched subject",
    )
    candidates, errors = await InvestigationEngine(deps).compile_actions(
        path, [intent], runtime, {},
    )
    assert not candidates
    assert any("atomic_execution_subject_mismatch" in error for error in errors), errors
    assert gateway.invocations == [] and llm.calls == []
    assert (await deps.path_store.get_active(str(runtime.run_id))).steps[0].status == "pending"
    # The same known subject is still executable, not an indiscriminate hold.
    candidates, errors = await InvestigationEngine(deps).compile_actions(
        path, [intent.model_copy(update={"target_node_id": world.node_id,
                                        "arguments": {"target_ref": world.host}})], runtime, {},
    )
    assert len(candidates) == 1 and errors == []


@pytest.mark.parametrize("status", [401, 404])
async def test_exact_subject_http_observation_can_complete_without_a_finding(status):
    from redteam.orchestration.completion import validate_completion
    from tests.unit.test_semantic_completion_receipts import (
        RUN,
        TARGET_NODE,
        _path,
        _path_step,
        _recording_world,
    )

    store, _, service, _, _, refs = await _recording_world(output={"status": status})
    plan = _path(_path_step(kind="observation"))
    original = (await store.get(refs[0])).model_dump(mode="json")
    errors = await validate_completion(
        path=plan, step=plan.steps[0],
        proposal={"outcome": "success", "evidence_ids": [refs[0]], "reason": "Status observed"},
        evidence_store=store, graph_service=service, run_id=str(RUN),
    )
    assert errors == []
    assert plan.steps[0].target_node_ids == [TARGET_NODE]
    assert (await store.get(refs[0])).model_dump(mode="json") == original
    assert await service.list_findings() == []
    # Observation success must not grant an exploit/vulnerability claim.
    plan.steps[0].completion_criteria[0].kind = "poc"
    assert await validate_completion(
        path=plan, step=plan.steps[0],
        proposal={"outcome": "success", "evidence_ids": [refs[0]], "reason": "Claimed exploit"},
        evidence_store=store, graph_service=service, run_id=str(RUN),
    )


async def test_other_subject_requires_both_grounded_relation_and_final_receipt():
    from redteam.attack_graph.identity import node_id_for_target
    from redteam.attack_graph.predicates import Confidence, Provenance
    from redteam.core.enums import GraphEdgeType, Verdict
    from redteam.orchestration.analysis import (
        VerificationAssessment,
        append_posthoc_claim_receipt,
    )
    from redteam.orchestration.completion import validate_completion
    from tests.unit.test_semantic_completion_receipts import (
        ENGAGEMENT,
        RUN,
        TARGET_NODE,
        _path,
        _path_step,
        _recording_context,
        _recording_world,
    )

    store, _, service, deps, registry, refs = await _recording_world()
    original = (await store.get(refs[0])).model_dump(mode="json")
    related = UrlTarget(url="https://other.local/api")
    identity = str(node_id_for_target(related))
    plan = _path(_path_step(kind="observation"))
    plan.steps[0].target_node_ids = [identity]
    proposal = {
        "outcome": "success", "reason": "Observe a related address", "evidence_ids": refs[:1],
    }

    async def check():
        return await validate_completion(
            path=plan, step=plan.steps[0], proposal=proposal,
            evidence_store=store, graph_service=service, run_id=str(RUN),
        )

    assert await check()  # Same execution purpose does not prove another object.
    # Use the existing recording owner to prove a direct address relation from raw data.
    recording = await registry.call("graph.record", {"facts": [{
        "evidence_id": refs[0], "raw_path": ["status"], "value": 200,
        "entity_raw_path": ["address"], "fact_name": "response_status",
        "target_node_id": TARGET_NODE,
        "new_entity": {"target": related.model_dump(mode="json"), "relation": "observed"},
    }]}, _recording_context(deps))
    assert recording.ok
    assert await check()  # Measured relation alone is not an admission receipt.
    source = await store.get(refs[0])
    receipt = await append_posthoc_claim_receipt(
        evidence_store=store, run_id=str(RUN), engagement_id=str(ENGAGEMENT),
        claim={"statement": "The raw source cites this address", "target_node_id": identity},
        evidence_ids=refs[:1], target_node_id=identity,
        action_id=str(source.action_id), invocation_id=str(source.invocation_id),
        assessment=VerificationAssessment(
            verdict=Verdict.PASS, outcome="success", reason="Fixture address review",
            claim_supported=True, source_refs=refs[:1], reviewer_id="fixture-review",
        ),
    )
    assert receipt is not None
    assert await check() == []
    assert (await store.get(refs[0])).model_dump(mode="json") == original
    assert await service.list_findings() == []
    # An inferred/wrong-source relation cannot extend the receipt to another object.
    unrelated = await service.observe_target(UrlTarget(url="https://other.local/unrelated"))
    await service.link(
        GraphEdgeType.OBSERVED, TARGET_NODE, unrelated.id,
        properties={"relation": {"type": "observed", "source_node_id": TARGET_NODE,
                                 "target_node_id": str(unrelated.id)}},
        provenance=Provenance(source="fixture", confidence=Confidence.INFERRED,
                              run_id=str(RUN), evidence_id=refs[0], step=0),
    )
    plan.steps[0].target_node_ids = [str(unrelated.id)]
    assert await check()
