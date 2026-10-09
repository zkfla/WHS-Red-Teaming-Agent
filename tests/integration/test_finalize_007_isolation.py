"""Finalize-007 isolation: one instance never closes foreign work.

Uses real mission/task stores plus the coordinator binding (actual
multi-instance mission state, not mocks). The scoped execution gate uses
the shared journal helper internally; these tests drive it through the
real finalize and engine paths.
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

from redteam.attack_graph.observation import GraphObservation
from redteam.core.contracts.evidence import EvidenceRecord
from redteam.core.contracts.mission import MissionPlan, MissionTask, Provenance
from redteam.core.enums import EvidenceSourceType, MissionStatus, TaskStatus
from redteam.core.ids import (
    ActionId,
    AgentId,
    AgentInstanceId,
    EngagementId,
    EvidenceId,
    GraphNodeId,
    InvocationId,
    MissionId,
    RunId,
    TaskId,
)
from redteam.event_bus.memory import InMemoryEventBus
from redteam.event_bus.publisher import EventPublisher
from redteam.orchestration.candidates.models import PriorityScore
from redteam.orchestration.coordination import coordinator_for
from redteam.orchestration.engine import InvestigationEngine
from redteam.orchestration.replanning import finalize_mission
from redteam.orchestration.semantic_completion import _pending_execution_errors
from redteam.orchestration.task_graph.memory import InMemoryTaskGraphStore
from redteam.orchestration.work.memory import InMemoryWorkStore
from redteam.orchestration.work.models import WorkItem, WorkStatus, work_owner_for
from tests.fakes import FakeEvidenceStore
from tests.integration import _engine_llm_harness as h

_HOLD = "an execution outcome is uncertain; collect its actual result or hold"
_UNFIN = "unfinished execution records remain"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _mission(mission_id: str) -> MissionPlan:
    return MissionPlan(
        mission_id=MissionId(mission_id),
        engagement_id=EngagementId("eng-007"),
        objective="assess isolation",
        status=MissionStatus.ACTIVE,
        provenance=Provenance(source="test"),
        created_at=_now(),
        updated_at=_now(),
    )


def _task(mission_id: str, task_id: str, status: TaskStatus) -> MissionTask:
    return MissionTask(
        task_id=TaskId(task_id),
        mission_id=MissionId(mission_id),
        title=task_id,
        objective="do it",
        status=status,
        provenance=Provenance(source="test"),
        created_at=_now(),
        updated_at=_now(),
    )


def _publisher(run_id: str) -> EventPublisher:
    return EventPublisher(
        InMemoryEventBus(),
        run_id=RunId(run_id),
        engagement_id=EngagementId("eng-007"),
        source="test.finalize007",
    )


def _work(work_id: str, run_id: str, instance: Any, task_id: str, status: WorkStatus) -> WorkItem:
    return WorkItem(
        work_id=work_id,
        run_id=RunId(run_id),
        candidate_key="candidate:" + work_id,
        agent_id=AgentId("agent"),
        instance_id=instance,
        mission_task_id=task_id,
        target_node_ids=[GraphNodeId("host:a.local")],
        priority=PriorityScore(total=1.0),
        created_at=_now(),
        updated_at=_now(),
        status=status,
    )


def _tool_record(
    evidence_id: str,
    run_id: str,
    action_id: str,
    invocation_id: str,
    instance: Any,
    task_id: str,
    uncertain: bool,
    reconciled: bool,
    code: str = "boom",
) -> EvidenceRecord:
    inner: dict[str, Any] = {}
    if uncertain:
        inner["execution_uncertain"] = True
    inner["execution_reconciled"] = reconciled
    return EvidenceRecord(
        evidence_id=EvidenceId(evidence_id),
        run_id=RunId(run_id),
        engagement_id=EngagementId("eng-007"),
        source_type=EvidenceSourceType.TOOL,
        source_id="tool.x",
        action_id=ActionId(action_id),
        invocation_id=InvocationId(invocation_id),
        content_type="application/json",
        digest="d-" + evidence_id,
        created_at=_now(),
        metadata={
            "tool_result": {"metadata": dict(inner), "error": {"code": code, "retryable": False}},
            "proposal": {"instance_id": instance},
            "mission_task_id": task_id,
            "invocation_id": invocation_id,
        },
    )


def _runtime(run_id: str, task_id: str | None, instance: Any) -> Any:
    rt = h.make_runtime(run_id, "eng-007", mission_id="m-007", task_id=task_id)
    return rt.model_copy(update={"instance_id": instance})


async def test_task_scope_ignores_foreign_work() -> None:
    work = InMemoryWorkStore()
    assert await work.add(_work("w-own", "run-007", "inst-a", "task-a", WorkStatus.RUNNING))
    assert await work.add(_work("w-foreign", "run-007", "inst-b", "task-b", WorkStatus.RUNNING))
    evidence = FakeEvidenceStore()
    engine = SimpleNamespace(deps=SimpleNamespace(work_store=work, evidence_store=evidence))
    runtime_a = _runtime("run-007", "task-a", "inst-a")
    task_a = _task("m-007", "task-a", TaskStatus.RUNNING)
    got = await _pending_execution_errors(engine, runtime_a, scope="task", task=task_a)
    assert got == [_UNFIN]
    work2 = InMemoryWorkStore()
    assert await work2.add(_work("w-done", "run-007", "inst-a", "task-a", WorkStatus.SUCCEEDED))
    assert await work2.add(_work("w-foreign", "run-007", "inst-b", "task-b", WorkStatus.RUNNING))
    engine2 = SimpleNamespace(deps=SimpleNamespace(work_store=work2, evidence_store=evidence))
    got = await _pending_execution_errors(
        engine2, runtime_a, scope="task", task=task_a
    )
    assert got == []


async def test_goal_scope_sees_all_work() -> None:
    work = InMemoryWorkStore()
    assert await work.add(_work("w-foreign", "run-007", "inst-b", "task-b", WorkStatus.RUNNING))
    evidence = FakeEvidenceStore()
    engine = SimpleNamespace(deps=SimpleNamespace(work_store=work, evidence_store=evidence))
    runtime_a = _runtime("run-007", "task-a", "inst-a")
    got = await _pending_execution_errors(
        engine, runtime_a, scope="goal", all_instances=True
    )
    assert got == [_UNFIN]


async def test_tool_uncertain_needs_exact_marker() -> None:
    work = InMemoryWorkStore()
    evidence = FakeEvidenceStore()
    await evidence.append(
        _tool_record(
            "ev-u",
            "run-007",
            "a-1",
            "inv-1",
            "inst-a",
            "task-a",
            True,
            False
        )
    )
    engine = SimpleNamespace(deps=SimpleNamespace(work_store=work, evidence_store=evidence))
    runtime_a = _runtime("run-007", "task-a", "inst-a")
    task_a = _task("m-007", "task-a", TaskStatus.RUNNING)
    got = await _pending_execution_errors(
        engine, runtime_a, scope="task", task=task_a
    )
    assert got == [_HOLD]
    await evidence.append(
        _tool_record(
            "ev-u2",
            "run-007",
            "a-2",
            "inv-2",
            "inst-a",
            "task-a",
            True,
            True
        )
    )
    got = await _pending_execution_errors(
        engine, runtime_a, scope="task", task=task_a
    )
    assert got == [_HOLD]
    await evidence.append(
        _tool_record(
            "ev-c",
            "run-007",
            "a-1",
            "inv-1",
            "inst-a",
            "task-a",
            False,
            True
        )
    )
    got = await _pending_execution_errors(
        engine, runtime_a, scope="task", task=task_a
    )
    assert got == [_HOLD]
    await evidence.append(
        _tool_record(
            "ev-f",
            "run-007",
            "a-9",
            "inv-9",
            "inst-b",
            "task-b",
            True,
            False
        )
    )
    got = await _pending_execution_errors(
        engine, runtime_a, scope="task", task=task_a
    )
    assert got == [_HOLD]


async def test_finalize_leaves_active_with_foreign_running() -> None:
    store = InMemoryTaskGraphStore()
    await store.create_mission(_mission("m-007"))
    await store.create_task(_task("m-007", "task-a", TaskStatus.READY))
    await store.create_task(_task("m-007", "task-b", TaskStatus.READY))
    await store.transition_task(TaskId("task-a"), TaskStatus.RUNNING, now=_now())
    await store.transition_task(TaskId("task-b"), TaskStatus.RUNNING, now=_now())
    owner_a = work_owner_for(RunId("run-007"), AgentInstanceId("inst-a"))
    owner_b = work_owner_for(RunId("run-007"), AgentInstanceId("inst-b"))
    ta = await store.get_task(TaskId("task-a"))
    tb = await store.get_task(TaskId("task-b"))
    assert ta is not None and tb is not None
    await store.update_task(ta.model_copy(update={"occupant_owner": owner_a, "updated_at": _now()}))
    await store.update_task(tb.model_copy(update={"occupant_owner": owner_b, "updated_at": _now()}))
    runtime_a = _runtime("run-007", "task-a", "inst-a")
    out = await finalize_mission(
        store,
        MissionId("m-007"),
        publisher=_publisher("run-007"),
        last_event_id=None,
        runtime=runtime_a,
        run_id=RunId("run-007"),
    )
    assert out is None
    mission = await store.get_mission(MissionId("m-007"))
    assert mission is not None and mission.status is MissionStatus.ACTIVE
    got_a = await store.get_task(TaskId("task-a"))
    got_b = await store.get_task(TaskId("task-b"))
    assert got_a is not None and got_a.status is TaskStatus.RUNNING
    assert got_b is not None and got_b.status is TaskStatus.RUNNING


async def test_settle_blocks_only_own_task() -> None:
    llm = h.ScriptedLLM()
    seed = await h.seed_host("run-007-unresolved")
    world = h.make_world(llm, graph_store=seed.store)
    mission, task_a = h.make_mission_task(
        mission_id="m-007-unresolved",
        engagement_id="eng-007",
        criteria=["actual response"],
        task_id="task-a",
    )
    mission.status = MissionStatus.ACTIVE
    task_a.status = TaskStatus.RUNNING
    await world.tasks.create_mission(mission)
    await world.tasks.create_task(task_a)
    task_b = MissionTask(
        task_id=TaskId("task-b"),
        mission_id=mission.mission_id,
        title="second",
        objective="second objective",
        status=TaskStatus.RUNNING,
        required=True,
        provenance=task_a.provenance,
        created_at=_now(),
        updated_at=_now(),
    )
    await world.tasks.create_task(task_b)
    owner_a = work_owner_for("run-007-unresolved", AgentInstanceId("inst-a"))
    owner_b = work_owner_for("run-007-unresolved", AgentInstanceId("inst-b"))
    sa = await world.tasks.get_task(TaskId("task-a"))
    sb = await world.tasks.get_task(TaskId("task-b"))
    assert sa is not None and sb is not None
    await world.tasks.update_task(
        sa.model_copy(update={"occupant_owner": owner_a, "updated_at": _now()})
    )
    await world.tasks.update_task(
        sb.model_copy(update={"occupant_owner": owner_b, "updated_at": _now()})
    )
    runtime_a = h.make_runtime(
        "run-007-unresolved",
        "eng-007",
        mission_id="m-007-unresolved",
        task_id="task-a",
    )
    runtime_a = runtime_a.model_copy(update={"instance_id": "inst-a"})
    await coordinator_for(world.tasks, world.paths).bind(
        str(runtime_a.run_id), str(mission.mission_id), str(mission.engagement_id)
    )
    raw = h.draft_path(seed.node_id)
    raw.update({
        "path_id": "path-a",
        "run_id": str(runtime_a.run_id),
        "version": 1,
        "status": "active",
        "mission_id": str(mission.mission_id),
        "mission_task_id": "task-a",
        "goal": task_a.objective,
    })
    from redteam.orchestration.paths import PathPlan as _PathPlan
    path = await world.paths.register(_PathPlan.model_validate(raw))
    await world.evidence.append(
        h.make_tool_evidence(
            evidence_id="proof-a",
            run_id=str(runtime_a.run_id),
            engagement_id="eng-007",
            action_id="action-a",
            invocation_id="inv-a",
            path_id=path.path_id,
            path_version=path.version,
            step_id="step-1",
            target_node_id=seed.node_id,
        )
    )
    engine = InvestigationEngine(world.deps)
    llm.add(
        "next_action",
        {
            "mode": "complete_task",
            "reason": "gap stays open",
            "actions": [],
            "path": None,
            "path_step_id": None,
            "completion": {
                "evidence_ids": ["proof-a"],
                "outcome": "blocked",
                "reason": "gap stays open",
                "criterion_results": [],
            },
            "evidence_request": {},
            "hypothesis_update": {},
        },
    )
    _, compiled, outcome = await engine.decide(runtime_a, GraphObservation())
    assert compiled == []
    assert outcome == "task_blocked"
    ga = await world.tasks.get_task(TaskId("task-a"))
    gb = await world.tasks.get_task(TaskId("task-b"))
    assert ga is not None and ga.status is TaskStatus.BLOCKED
    assert gb is not None and gb.status is TaskStatus.RUNNING
    gm = await world.tasks.get_mission(mission.mission_id)
    assert gm is not None and gm.status is MissionStatus.ACTIVE


async def test_global_needs_receipts_and_failure_preserved() -> None:
    store = InMemoryTaskGraphStore()
    await store.create_mission(_mission("m-007"))
    await store.create_task(_task("m-007", "task-a", TaskStatus.READY))
    await store.create_task(_task("m-007", "task-b", TaskStatus.READY))
    await store.transition_task(TaskId("task-a"), TaskStatus.RUNNING, now=_now())
    await store.transition_task(TaskId("task-b"), TaskStatus.RUNNING, now=_now())
    await store.transition_task(TaskId("task-a"), TaskStatus.COMPLETED, now=_now())
    await finalize_mission(
        store, MissionId("m-007"), publisher=_publisher("run-007"), last_event_id=None
    )
    m = await store.get_mission(MissionId("m-007"))
    assert m is not None and m.status is MissionStatus.BLOCKED
    store2 = InMemoryTaskGraphStore()
    await store2.create_mission(_mission("m-007"))
    await store2.create_task(_task("m-007", "task-a", TaskStatus.READY))
    await store2.transition_task(TaskId("task-a"), TaskStatus.RUNNING, now=_now())
    await store2.transition_task(TaskId("task-a"), TaskStatus.FAILED, now=_now())
    await finalize_mission(
        store2, MissionId("m-007"), publisher=_publisher("run-007"), last_event_id=None
    )
    m2 = await store2.get_mission(MissionId("m-007"))
    assert m2 is not None and m2.status is MissionStatus.FAILED
