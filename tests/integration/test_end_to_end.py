"""Production loop and event chain, with explicit model and gateway test doubles."""

from __future__ import annotations

import pytest

from redteam.app.bootstrap import build_runtime_deps, run_engagement
from redteam.attack_graph.predicates import facts_of
from redteam.attack_graph.stores.memory import InMemoryAttackGraphStore
from redteam.core.contracts.events import EventEnvelope, EventType
from redteam.core.enums import AgentPhase, MissionStatus, Verdict
from redteam.core.ids import GraphNodeId
from redteam.orchestration.events import REPLAN_TRIGGERED, REPORT_PROJECTED
from redteam.orchestration.state import TerminationReason
from tests.integration import _engine_llm_harness as h
from tests.integration.test_engine_live_run_flow import CountingGateway, LiveScriptedLLM, _live_deps


def _engagement():
    return h.make_engagement(engagement_id="eng_e2e")


def _world():
    engagement = _engagement()
    llm = LiveScriptedLLM(
        graph_store=InMemoryAttackGraphStore(), evidence_store=h.FakeEvidenceStore()
    )
    gateway = CountingGateway()
    return engagement, _live_deps(llm, gateway, engagement), gateway


async def test_minimal_vertical_slice_end_to_end(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    engagement, deps, gateway = _world()
    events: list[EventEnvelope] = []

    async def collect(event: EventEnvelope) -> None:
        events.append(event)

    await deps.event_bus.subscribe("*", collect)
    final = await run_engagement(engagement, deps)
    assert final.phase is AgentPhase.FINISHED
    assert final.termination_reason == TerminationReason.GOAL_ACHIEVED
    assert final.last_verdict is Verdict.UNKNOWN
    assert final.actions_executed == len(gateway.calls) == 1
    mission = await deps.task_graph_store.get_mission(final.mission_id)
    assert mission is not None and mission.status is MissionStatus.COMPLETED
    types = [event.event_type for event in events]
    required_order = [
        EventType.RUN_STARTED,
        EventType.MISSION_CREATED,
        EventType.TASK_CREATED,
        EventType.TASK_STARTED,
        EventType.AGENT_STARTED,
        EventType.ACTION_PROPOSED,
        EventType.POLICY_EVALUATED,
        EventType.ACTION_APPROVED,
        EventType.TOOL_EXECUTION_STARTED,
        EventType.TOOL_EXECUTION_FINISHED,
        EventType.EVIDENCE_CREATED,
        "ObservationRecorded",
        EventType.GRAPH_UPDATED,
        REPLAN_TRIGGERED,
        EventType.TASK_COMPLETED,
        EventType.MISSION_UPDATED,
        REPORT_PROJECTED,
        EventType.RUN_FINISHED,
    ]
    cursor = 0
    for kind in required_order:
        cursor = types.index(kind, cursor) + 1
    assert all(event.run_id == final.run_id for event in events)
    assert all(event.engagement_id == engagement.engagement_id for event in events)
    proposal = next(event for event in events if event.event_type == EventType.ACTION_PROPOSED)
    evidence = await deps.evidence_store.list_for_run(final.run_id)
    raw = [record for record in evidence if isinstance(record.metadata.get("tool_result"), dict)]
    assert len(raw) == 1 and raw[0].action_id == proposal.payload["action_id"]
    assert raw[0].invocation_id == gateway.calls[0].invocation_id
    path = await deps.path_store.get_active(str(final.run_id))
    assert path is not None and path.steps[0].evidence_ids
    node = await deps.graph_store.get_node(GraphNodeId(f"host:{h.HOST}"))
    assert node is not None
    assert h.CANONICAL_OP in facts_of(node.properties)["attempted_operations"]
    assert (tmp_path / "data" / "reports" / f"{final.run_id}.json").exists()


async def test_quick_start_objective_reaches_mission_without_secret(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    engagement, deps, gateway = _world()
    final = await run_engagement(
        engagement, deps, objective="VulnBank 웹 레드티밍, 인증/인가 중심. token=privatevalue"
    )
    mission = await deps.task_graph_store.get_mission(final.mission_id)
    assert mission is not None
    assert mission.priorities[:2] == ["authentication", "authorization"]
    assert "privatevalue" not in mission.model_dump_json()
    assert len(gateway.calls) == 1
    path = await deps.path_store.get_active(str(final.run_id))
    assert path is not None and path.goal == mission.objective


async def test_bare_composition_requires_explicit_llm(tmp_path) -> None:
    with pytest.raises(TypeError, match="llm"):
        await build_runtime_deps(_engagement(), evidence_dir=tmp_path)  # type: ignore[call-arg]
    with pytest.raises(ValueError, match="LLM"):
        await build_runtime_deps(_engagement(), evidence_dir=tmp_path, llm=None)  # type: ignore[arg-type]
