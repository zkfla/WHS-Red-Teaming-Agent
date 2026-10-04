"""Merged runtime regression: production LLM path only (no network/clock)."""
from __future__ import annotations

import copy
import json

from redteam.attack_graph.observation import GraphObservation
from redteam.core.contracts.events import EventType
from redteam.core.enums import MissionStatus, OperatorIntentType
from redteam.core.ids import AgentId
from redteam.orchestration.agents.providers import PlaybookCandidateProvider
from redteam.orchestration.agents.registry import default_registry
from redteam.orchestration.analysis import DeterministicAnalysisVerifier
from redteam.orchestration.context import RuntimeDeps
from redteam.orchestration.engine import InvestigationEngine, operator_epoch
from redteam.orchestration.events import (
    AGENT_CONTEXT_PREPARED,
    AGENT_DECISION_RECORDED,
    AGENT_TURN_DECIDED,
    MCP_TOOL_SELECTED,
)
from redteam.orchestration.evidence_access import EvidenceReader
from redteam.orchestration.nodes.plan import plan_node
from redteam.orchestration.observations import default_result_mapper_registry
from redteam.orchestration.operator_intents import (
    InMemoryOperatorIntentStore,
    OperatorIntentService,
)
from redteam.orchestration.runtime_observation import (
    RuntimeObservation,
    append_runtime_observation,
)
from redteam.orchestration.settings import OrchestrationSettings
from redteam.orchestration.state import initial_graph_state
from tests.fakes import DummyToolGateway, FakeControlPlane
from tests.integration import _engine_llm_harness as h


def _real_deps(llm, *, seed, world, intent_store=None):
    gateway = DummyToolGateway(tools=[h.inventory_descriptor()])
    deps = RuntimeDeps(
        control_plane=FakeControlPlane(),
        tool_gateway=gateway,
        event_bus=world.deps.event_bus,
        graph_store=seed.store,
        evidence_store=world.evidence,
        llm_client=llm,
        verifier=DeterministicAnalysisVerifier(),
        engagement=seed.engagement,
        agents=default_registry(),
        tool_catalog=gateway,
        result_mappers=default_result_mapper_registry(),
        work_store=world.work,
        task_graph_store=world.tasks,
        path_store=world.paths,
        operator_intent_store=intent_store,
        settings=OrchestrationSettings(decision_max_attempts=3),
    )
    return deps, gateway


def _payload(call):
    content = call["messages"][0]["content"]
    body = content.split("\n\nPrevious attempt")[0]
    return json.loads(body)


async def test_bad_compile_repairs_then_good_compiles_with_shared_budget():
    llm = h.ScriptedLLM()
    seed = await h.seed_host("run-merged-badgood")
    world = h.make_world(llm, graph_store=seed.store, decision_max_attempts=3)
    deps, _gateway = _real_deps(llm, seed=seed, world=world)
    runtime = h.make_runtime("run-merged-badgood", "eng-t1")
    engine = InvestigationEngine(deps)

    spec = deps.agents.get(AgentId(h.RECON_AGENT))
    assert spec is not None
    drafts = await PlaybookCandidateProvider().propose(
        spec, seed.service, GraphObservation(), step=0
    )
    assert h.INVENTORY_TOOL not in {str(d.plan_step.tool_id) for d in drafts}

    bad = copy.deepcopy(h.turn_actions(seed.node_id))
    bad["actions"][0]["tool_id"] = "recon.unknown_tool"
    bad["actions"][0]["reason"] = "bad tool outside catalog"
    llm.add("path_draft", h.draft_path(seed.node_id))
    llm.add("path_review", h.review_accept())
    llm.add("next_action", bad)
    llm.add("next_action", h.turn_actions(seed.node_id))

    path, compiled, note = await engine.decide(runtime, GraphObservation())

    assert note is None
    assert len(compiled) == 1
    assert str(compiled[0].tool.tool_id) == h.INVENTORY_TOOL
    assert str(compiled[0].plan_step.operation_key) == h.CANONICAL_OP
    routes = [c["route"] for c in llm.calls]
    assert routes == ["path_draft", "path_review", "next_action", "next_action"]
    assert len(llm.calls) == 4
    first_next = _payload(llm.calls[2])
    second_next = _payload(llm.calls[3])
    assert "repair_references" not in first_next
    assert isinstance(second_next.get("repair_references"), dict)
    assert set(("items", "omitted_count")) <= set(second_next["repair_references"])
    assert second_next.get("feedback")


async def test_context_keeps_raw_but_projects_public_allowlist():
    llm = h.ScriptedLLM()
    seed = await h.seed_host("run-merged-ctx")
    world = h.make_world(llm, graph_store=seed.store, decision_max_attempts=3)
    deps, _ = _real_deps(llm, seed=seed, world=world)
    runtime = h.make_runtime("run-merged-ctx", "eng-t1")
    runtime = append_runtime_observation(
        runtime,
        RuntimeObservation(
            kind="control",
            summary="Control ALLOW for recon.service_discovery",
            outcome="allow",
            step=0,
            action_id="action-ctx-1",
        ),
    )
    base = h.make_tool_evidence(
        evidence_id="ev-ctx-1",
        run_id="run-merged-ctx",
        engagement_id="eng-t1",
        action_id="action-ctx-1",
        invocation_id="inv-ctx-1",
        path_id="path-ctx",
        path_version=1,
        step_id="step-1",
        target_node_id=seed.node_id,
    )
    base.metadata.update(
        {
            "invocation": {"tool_id": h.INVENTORY_TOOL, "args": {"x": 1}},
            "arguments": {"token": "abc123"},
            "headers": {"Authorization": "Bearer abc123"},
            "proposal": {"action_id": "action-ctx-1"},
            "plan_step": {"step_id": "step-1"},
            "mystery_extra": "drop-me",
            "reason": "token=super-secret-abc",
        }
    )
    await world.evidence.append(base)
    engine = InvestigationEngine(deps)
    ctx = await engine.context(runtime, GraphObservation())

    stored = await world.evidence.get("ev-ctx-1")
    assert stored is not None
    assert stored.metadata["invocation"]["tool_id"] == h.INVENTORY_TOOL
    assert stored.metadata["mystery_extra"] == "drop-me"
    assert "super-secret-abc" in str(stored.metadata.get("reason", ""))

    entry = next(e for e in ctx["evidence"] if e["evidence_id"] == "ev-ctx-1")
    projected = entry["metadata"]
    for forbidden in (
        "invocation",
        "arguments",
        "headers",
        "tool_result",
        "proposal",
        "plan_step",
        "mystery_extra",
    ):
        assert forbidden not in projected
    assert projected["action_id"] == "action-ctx-1"
    assert projected["invocation_id"] == "inv-ctx-1"
    assert "super-secret-abc" not in json.dumps(projected)
    assert ctx["evidence_count"] == 1
    assert ctx["evidence_omitted_count"] == 0
    assert "ev-ctx-1" in ctx["evidence_index"]
    assert len(ctx["runtime_observations"]) == 1
    assert ctx["runtime_observations"][0]["action_id"] == "action-ctx-1"

    reader = EvidenceReader(world.evidence)
    window = await reader.read("ev-ctx-1", run_id="run-merged-ctx", limit=4000)
    assert window["status"] == "ok"
    assert window["text"]


async def test_plan_node_event_causation_and_planning_failed_cause():
    llm = h.ScriptedLLM()
    seed = await h.seed_host("run-merged-events")
    world = h.make_world(llm, graph_store=seed.store, decision_max_attempts=3)
    deps, _ = _real_deps(llm, seed=seed, world=world)
    runtime = h.make_runtime("run-merged-events", "eng-t1")
    llm.add("path_draft", h.draft_path(seed.node_id))
    llm.add("path_review", h.review_accept())
    llm.add("next_action", h.turn_actions(seed.node_id))
    state = await plan_node(
        {**initial_graph_state(runtime), "observation": GraphObservation()}, deps=deps
    )

    assert state["action_proposal"] is not None
    proposal = state["action_proposal"]
    step = state["plan_step"]
    assert step is not None
    history = deps.event_bus.history()
    by_type = {}
    for event in history:
        by_type.setdefault(event.event_type, []).append(event)
    for name in (
        AGENT_CONTEXT_PREPARED,
        AGENT_TURN_DECIDED,
        AGENT_DECISION_RECORDED,
        MCP_TOOL_SELECTED,
        EventType.ACTION_PROPOSED,
    ):
        assert by_type.get(name), name
    proposed = by_type[EventType.ACTION_PROPOSED][-1]
    selected = by_type[MCP_TOOL_SELECTED][-1]
    recorded = by_type[AGENT_DECISION_RECORDED][-1]
    assert proposed.causation_id == selected.event_id
    assert selected.causation_id == recorded.event_id
    decided_ids = {e.event_id for e in by_type[AGENT_TURN_DECIDED]}
    assert recorded.causation_id in decided_ids
    assert proposed.payload["action_id"] == str(proposal.action_id)
    assert proposed.payload["path_id"] == step.path_id
    assert proposed.payload["path_step_id"] == step.path_step_id

    fail_llm = h.ScriptedLLM()
    fail_seed = await h.seed_host("run-merged-planfail")
    fail_world = h.make_world(fail_llm, graph_store=fail_seed.store, decision_max_attempts=3)
    fail_deps, _ = _real_deps(fail_llm, seed=fail_seed, world=fail_world)
    fail_runtime = h.make_runtime("run-merged-planfail", "eng-t1")
    bad = copy.deepcopy(h.turn_actions(fail_seed.node_id))
    bad["actions"][0]["existing_hypothesis_id"] = "hyp-missing"
    fail_llm.add("path_draft", h.draft_path(fail_seed.node_id))
    fail_llm.add("path_review", h.review_accept())
    fail_llm.add("next_action", copy.deepcopy(bad))
    fail_llm.add("next_action", copy.deepcopy(bad))
    fail_llm.add("next_action", copy.deepcopy(bad))
    fail_state = await plan_node(
        {**initial_graph_state(fail_runtime), "observation": GraphObservation()},
        deps=fail_deps,
    )
    assert fail_state["action_proposal"] is None
    fail_history = fail_deps.event_bus.history()
    failed = [e for e in fail_history if e.event_type == "PlanningFailed"]
    assert failed
    decided = [e for e in fail_history if e.event_type == AGENT_TURN_DECIDED]
    assert decided
    assert failed[-1].causation_id in {e.event_id for e in decided}


async def test_guidance_keeps_epoch_but_exclude_moves_it():
    llm = h.ScriptedLLM()
    seed = await h.seed_host("run-merged-guide")
    world = h.make_world(llm, graph_store=seed.store, decision_max_attempts=3)
    intents = InMemoryOperatorIntentStore()
    deps, _ = _real_deps(llm, seed=seed, world=world, intent_store=intents)
    mission, task = h.make_mission_task(
        mission_id="mission-merged",
        engagement_id="eng-t1",
        criteria=["evidence-backed result"],
    )
    mission = mission.model_copy(update={"status": MissionStatus.ACTIVE})
    await world.tasks.create_mission(mission)
    await world.tasks.create_task(task)
    runtime = h.make_runtime(
        "run-merged-guide", "eng-t1", mission_id="mission-merged", task_id="task-t1"
    )
    from redteam.orchestration.coordination import coordinator_for

    await coordinator_for(world.tasks, world.paths).bind(
        str(runtime.run_id), str(mission.mission_id), str(mission.engagement_id)
    )
    svc = OperatorIntentService(
        store=intents,
        task_graph_store=world.tasks,
        event_bus=deps.event_bus,
        graph_store=seed.store,
        work_store=world.work,
    )

    before = await operator_epoch(deps, runtime)
    guided = await svc.receive_guidance_and_apply(
        "advice mentions stop word here",
        mission_id=mission.mission_id,
        run_id=runtime.run_id,
    )
    assert guided.intent_type is OperatorIntentType.GUIDANCE
    assert await operator_epoch(deps, runtime) == before

    ctx = await InvestigationEngine(deps).context(runtime, GraphObservation())
    texts = []
    for item in ctx.get("operator_instructions", []):
        texts.append(str(item.get("intent", {}).get("instruction", "")))
        texts.append(str(item.get("instruction", "")))
    assert any("stop" in text.lower() for text in texts)

    excluded = await svc.receive_and_apply(
        "exclude mobile", mission_id=mission.mission_id, run_id=runtime.run_id
    )
    assert excluded.intent_type is not OperatorIntentType.GUIDANCE
    after = await operator_epoch(deps, runtime)
    assert after is not None
    assert after != before
