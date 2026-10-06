"""Explicit fixture LLM -> real serial roles/Control/stdio MCP -> loopback bank."""

import json
from contextlib import AsyncExitStack

import pytest

from redteam.app.bootstrap import build_application
from redteam.attack_graph.observation import GraphObservation
from redteam.attack_graph.service import AttackGraphService
from redteam.core.enums import PolicyVerdict
from redteam.core.ids import RunId
from redteam.orchestration.engine import InvestigationEngine
from redteam.orchestration.nodes import aggregate_node
from redteam.orchestration.nodes.plan import plan_node
from redteam.orchestration.nodes.worker import worker_node
from redteam.orchestration.seed import seed_from_engagement
from redteam.orchestration.state import initial_graph_state
from tests.integration import _engine_llm_harness as h
from tests.integration.test_approved_mcp_deployment import bank as bank_fixture
from tests.integration.test_approved_mcp_deployment import install
from tests.unit.test_specialist_profile import (
    PROFILE,
    WEB_ROLE,
    SpecialistLLM,
    _atomized,
    _strategy,
)

bank = bank_fixture


@pytest.mark.parametrize("require_approval", [False, True])
async def test_specialist_serial_real_stdio_scoped_comparison(
    tmp_path, monkeypatch, bank, require_approval,
):
    origin, requests = bank
    config, _ = install(
        tmp_path, monkeypatch, origin, require_approval=require_approval, port_pinned=True,
    )
    config = config.model_copy(update={"specialist_profile": str(PROFILE)})
    llm = SpecialistLLM()
    async with AsyncExitStack() as stack:
        engagement, deps = await build_application(
            config, llm=llm, mcp_stack=stack,
            evidence_dir=tmp_path / "evidence", audit_dir=tmp_path / "audit",
            approval_dir=tmp_path / "approvals",
        )
        runtime = h.make_runtime("run-specialist-live", str(engagement.engagement_id))
        nodes = await seed_from_engagement(
            AttackGraphService(deps.graph_store), engagement, run_id=RunId(runtime.run_id), step=0,
        )
        node = next(str(item.id) for item in nodes if str(item.id).startswith("host:"))
        strategy = _strategy(node)
        strategy["questions"][0]["principal_refs"] = ["alice", "bob"]
        atomized = _atomized(node)
        atomized["path"]["steps"][0]["principal_refs"] = ["alice", "bob"]
        llm.add("attack_path_proposal", strategy)
        llm.add("task_atomization", atomized)
        llm.add("path_review", h.review_accept())
        llm.add("next_action", {
            "mode": "delegate", "reason": "Only compare approved synthetic accounts",
            "delegations": [{"path_step_id": "s1", "agent_id": WEB_ROLE, "budget": {
                "max_actions": 2,
            }}],
        })
        engine = InvestigationEngine(deps)
        path, actions, note = await engine.decide(runtime, GraphObservation())
        assert path is not None and not actions and note == "continue_planning"
        assert requests == []
        comparison = deps.tool_catalog.get("web.http_compare_sessions")
        assert comparison is not None
        llm.add("worker_next_action", {
            "decision": "propose_actions", "reason": "One bounded synthetic comparison",
            "actions": [{
                "target_node_id": node, "principal_ref": "alice",
                "tool_id": str(comparison.tool_id),
                "arguments": {
                    "target_ref": "bank-test", "path": "/accounts/123",
                    "first_session_ref": "alice", "second_session_ref": "bob",
                },
                "reason": "Observe registered synthetic principals only",
                "hypothesis": {
                    "description": "The synthetic principal comparison produced observations",
                    "violation_if_confirmed": False,
                },
                "expectation": {
                    "kind": "output_field_present", "field": "first",
                    "description": "First comparison observation recorded",
                },
            }],
        })
        planned = await plan_node(
            {**initial_graph_state(engine.updated_runtime), "observation": GraphObservation()},
            deps=deps,
        )
        assert len(planned["dispatch_items"]) == 1
        item = planned["dispatch_items"][0]
        llm.add("analysis_verify", h.meaning("inconclusive", False))
        result = await worker_node(
            {"runtime": planned["runtime"], "dispatch_item": item}, deps=deps,
        )
        outcome = result["worker_outcomes"][str(item.proposal.action_id)]
        if require_approval:
            assert outcome.policy_verdict is PolicyVerdict.REQUIRE_APPROVAL
            assert not outcome.executed and requests == []
        else:
            assert outcome.policy_verdict is PolicyVerdict.ALLOW
            assert outcome.executed and outcome.succeeded
            assert requests == ["/accounts/123", "/accounts/123"]
            aggregate = await aggregate_node(
                {**planned, "worker_outcomes": result["worker_outcomes"]}, deps=deps,
            )
            assert aggregate["runtime"].actions_executed == 1
            records = await deps.evidence_store.list_for_run(runtime.run_id)
            exposed = json.dumps([llm.calls, [item.model_dump(mode="json") for item in records]])
            for secret in ("synthetic-a", "synthetic-b", "BODY_MUST_NOT_REACH_AGENT"):
                assert secret not in exposed
            review = next(call for call in llm.calls if call["route"] == "analysis_verify")
            assert "주장과 증거의 현재 유효성" in review["system"]


@pytest.mark.parametrize("mode", ["step", "task", "goal"])
@pytest.mark.parametrize("changed_during_review", [False, True])
async def test_specialist_completion_uses_upstream_context_and_atomic_commit(
    mode, changed_during_review,
):
    """Fixture evidence/model, real completion gates and upstream CAS; no target calls."""
    from redteam.app.agent_profile import load_specialist_profile
    from redteam.core.enums import MissionStatus, TaskStatus
    from redteam.orchestration.coordination import StateConflict
    from redteam.orchestration.engine import TurnDecision
    from redteam.orchestration.specialist_planning import SerialSpecialistPlanningProvider
    from tests.integration.test_completion_review_atomicity import setup

    llm = SpecialistLLM()
    world, runtime, path, _ = await setup(mode, llm)
    loaded = load_specialist_profile(PROFILE)
    world.deps.planning_provider = SerialSpecialistPlanningProvider(loaded.prompts)
    stage = {
        "step": "completion_review",
        "task": "task_completion_review",
        "goal": "goal_completion_review",
    }[mode]
    reply = {
        "step": h.completion_accept(),
        "task": h.task_accept(["proof-race"]),
        "goal": h.goal_accept(llm.criteria),
    }[mode]
    llm.add(stage, reply)
    engine = InvestigationEngine(world.deps)
    context = await engine.context(runtime, GraphObservation())
    context.update({
        "feedback": ["unrelated planning error"],
        "requested_evidence": [
            {"evidence_id": "proof-race", "text": "already-read stored observation"},
            {"evidence_id": "other", "text": "unrelated observation"},
        ],
    })
    if changed_during_review:
        original_call = llm.complete_structured

        async def update_task(**kwargs):
            await world.tasks.update_priority(runtime.active_task_id, 321, now=h.utcnow())
            return await original_call(**kwargs)

        llm.complete_structured = update_task

    async def complete():
        if mode == "step":
            return await engine.complete_step(
                path, TurnDecision.model_validate(h.turn_complete_step(["proof-race"])),
                runtime, context,
            )
        if mode == "task":
            return await engine.complete_task(
                path, TurnDecision(mode="complete_task", reason="evaluate actual evidence"),
                runtime, context,
            )
        return await engine.complete_goal(
            path, TurnDecision.model_validate(h.turn_complete_goal()), runtime, context,
        )

    if changed_during_review:
        with pytest.raises(StateConflict):
            await complete()
        expected = TaskStatus.COMPLETED if mode == "goal" else TaskStatus.RUNNING
        assert (await world.tasks.get_task(runtime.active_task_id)).status is expected
        assert (await world.tasks.get_mission(runtime.mission_id)).status is MissionStatus.ACTIVE
    else:
        result = await complete()
        assert (result[1] if mode == "step" else result) == []
        if mode == "task":
            task = await world.tasks.get_task(runtime.active_task_id)
            assert task.status is TaskStatus.COMPLETED
        if mode == "goal":
            mission = await world.tasks.get_mission(runtime.mission_id)
            assert mission.status is MissionStatus.COMPLETED
    assert len(llm.calls) == 1
    call = llm.calls[0]
    assert call["route"] == stage
    assert "주장과 증거의 현재 유효성" in call["system"]
    payload = json.loads(call["messages"][0]["content"])
    assert payload["decision_role"] == loaded.bindings["research-evidence-agent"]
    assert "tools" not in payload and "feedback" not in payload
    assert "steps" not in payload["active_path"]
    assert [item["evidence_id"] for item in payload["requested_evidence"]] == ["proof-race"]
    if mode == "step":
        assert payload["source_evidence_ids"] == ["proof-race"]
        assert "Accepting a step does not complete its task or mission" in call["system"]
    else:
        assert payload["verified_source_ids"] == ["proof-race"]
        assert payload["review_note"]
        assert "Describe actual results as claims" in call["system"]
