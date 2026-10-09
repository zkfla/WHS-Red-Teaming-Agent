"""Runner unsafe-checkpoint reconcile path (no replay, held stays held)."""

from __future__ import annotations

import asyncio

from redteam.attack_graph.service import AttackGraphService
from redteam.core.enums import MissionStatus
from redteam.core.ids import MissionId
from redteam.orchestration.checkpointing import build_checkpointer
from redteam.orchestration.runner import AgentLoopRunner
from redteam.orchestration.seed import seed_from_engagement
from tests.fakes import DummyToolGateway, FakeEvidenceStore
from tests.integration import _engine_llm_harness as h
from tests.integration.test_instance_driver_react import (
    _engagement,
    _mission_world,
    _react_deps,
    _runtime,
    _TimingGateway,
)


class _BlockingGateway(DummyToolGateway):
    def __init__(self) -> None:
        super().__init__(tools=[h.inventory_descriptor()])
        self.started = asyncio.Event()

    async def invoke(self, invocation):
        self.started.set()
        await asyncio.Event().wait()
        return await super().invoke(invocation)


async def test_missing_checkpoint_yields_none() -> None:
    engagement = _engagement("a.local")
    deps = _react_deps(engagement, _TimingGateway())
    saver = build_checkpointer(enabled=True, registry=deps.state_codecs)
    runner = AgentLoopRunner(deps, settings=deps.settings, checkpointer=saver)
    assert await runner.recover_reconciling("run-absent", instance_id="a") == (None, None)


async def test_safe_checkpoint_returns_no_reconciliation() -> None:
    engagement = _engagement("a.local")
    deps = _react_deps(engagement, _TimingGateway())
    await seed_from_engagement(AttackGraphService(deps.graph_store), engagement)
    saver = build_checkpointer(enabled=True, registry=deps.state_codecs)
    goal = "map the in-scope host"
    first = AgentLoopRunner(deps, settings=deps.settings, checkpointer=saver)
    # Goal-only: no mission binding exists, so the terminal snapshot stays
    # safe and recoverable (a completed mission would close its binding).
    final = await first.run(_runtime("run-safe", str(engagement.engagement_id), "a", goal))
    assert final.termination_reason == "stalled_no_graph_progress"
    second = AgentLoopRunner(deps, settings=deps.settings, checkpointer=saver)
    runtime, reconciliation = await second.recover_reconciling("run-safe", instance_id="a")
    assert reconciliation is None
    assert runtime is not None
    assert str(runtime.run_id) == "run-safe"


async def test_unsafe_checkpoint_held_pending_unknown() -> None:
    engagement = _engagement("a.local")
    gateway = _BlockingGateway()
    deps = _react_deps(engagement, gateway)
    await seed_from_engagement(AttackGraphService(deps.graph_store), engagement)
    mission_id = await _mission_world(deps, ["task-u1"])
    saver = build_checkpointer(enabled=True, registry=deps.state_codecs)
    goal = "map the in-scope host"
    first = AgentLoopRunner(deps, settings=deps.settings, checkpointer=saver)
    flight = asyncio.create_task(
        first.run(_runtime("run-unsafe", str(engagement.engagement_id), "a", goal, mission_id))
    )
    await asyncio.wait_for(gateway.started.wait(), timeout=30)
    flight.cancel()
    await asyncio.gather(flight, return_exceptions=True)
    second = AgentLoopRunner(deps, settings=deps.settings, checkpointer=saver)
    runtime, reconciliation = await second.recover_reconciling("run-unsafe", instance_id="a")
    assert runtime is not None
    assert reconciliation is not None
    assert not reconciliation.ready
    assert reconciliation.pending_invocations
    # Held identity preserved: no fresh-OBSERVE rewrite happened here.
    assert runtime.termination_reason is None


def test_snapshot_pending_work_ids_from_snapshot_fields() -> None:
    from types import SimpleNamespace

    from redteam.orchestration.runner import _snapshot_pending_work_ids

    assert _snapshot_pending_work_ids(None) == set()
    assert _snapshot_pending_work_ids({}) == set()
    values = {
        "current_work_id": "w-current",
        "native_calls": [
            {"completed": False, "dispatch": {"work_id": "w-native"}},
            {"completed": True, "dispatch": {"work_id": "w-done"}},
            {"completed": False, "dispatch": None},
            SimpleNamespace(
                completed=False, dispatch=SimpleNamespace(work_id="w-model")
            ),
        ],
        "dispatch_items": [{"work_id": "w-batch"}, {"other": 1}],
    }
    assert _snapshot_pending_work_ids(values) == {
        "w-current",
        "w-native",
        "w-model",
        "w-batch",
    }


async def test_ready_reconciliation_returned_verbatim(monkeypatch) -> None:
    from redteam.orchestration import execution_reconciliation as er_module
    from redteam.orchestration.execution_reconciliation import ExecutionReconciliation

    engagement = _engagement("a.local")
    gateway = _BlockingGateway()
    deps = _react_deps(engagement, gateway)
    await seed_from_engagement(AttackGraphService(deps.graph_store), engagement)
    mission_id = await _mission_world(deps, ["task-u2"])
    saver = build_checkpointer(enabled=True, registry=deps.state_codecs)
    goal = "map the in-scope host"
    first = AgentLoopRunner(deps, settings=deps.settings, checkpointer=saver)
    flight = asyncio.create_task(
        first.run(_runtime("run-ready", str(engagement.engagement_id), "a", goal, mission_id))
    )
    await asyncio.wait_for(gateway.started.wait(), timeout=30)
    flight.cancel()
    await asyncio.gather(flight, return_exceptions=True)

    async def fake_reconcile(deps, runtime):
        return ExecutionReconciliation(
            runtime=runtime, processed_invocations=["inv-1"]
        )

    monkeypatch.setattr(er_module, "reconcile_execution_receipts", fake_reconcile)
    second = AgentLoopRunner(deps, settings=deps.settings, checkpointer=saver)
    runtime, reconciliation = await second.recover_reconciling("run-ready", instance_id="a")
    assert runtime is not None
    assert reconciliation is not None
    assert reconciliation.ready
    assert reconciliation.processed_invocations == ["inv-1"]
    # Real submissions cover the snapshot's pending work: no coverage hold.
    assert "coverage" not in reconciliation.errors


async def test_pre_journal_unsafe_checkpoint_held_without_submission() -> None:
    # Old-checkpoint simulation: the snapshot still owes work outcomes,
    # but the journal is silent (effect-start submission predates it).
    # Zero submissions with a pending tools step proves nothing -- HOLD
    # with a type-only diagnostic, never resubmit, never fresh-OBSERVE.
    engagement = _engagement("a.local")
    gateway = _BlockingGateway()
    evidence = FakeEvidenceStore()
    deps = _react_deps(engagement, gateway, evidence)
    await seed_from_engagement(AttackGraphService(deps.graph_store), engagement)
    mission_id = await _mission_world(deps, ["task-j1"])
    saver = build_checkpointer(enabled=True, registry=deps.state_codecs)
    goal = "map the in-scope host"
    first = AgentLoopRunner(deps, settings=deps.settings, checkpointer=saver)
    flight = asyncio.create_task(
        first.run(_runtime("run-old", str(engagement.engagement_id), "a", goal, mission_id))
    )
    await asyncio.wait_for(gateway.started.wait(), timeout=30)
    flight.cancel()
    await asyncio.gather(flight, return_exceptions=True)
    submissions = [
        record
        for record in evidence.records
        if record.metadata.get("receipt_kind") == "execution_submission"
    ]
    assert submissions, "the blocked flight must submit before invoking"
    # Simulate the pre-journal era: snapshot pending, journal silent.
    evidence.records[:] = [
        record
        for record in evidence.records
        if record.metadata.get("receipt_kind") != "execution_submission"
    ]
    second = AgentLoopRunner(deps, settings=deps.settings, checkpointer=saver)
    runtime, reconciliation = await second.recover_reconciling("run-old", instance_id="a")
    assert runtime is not None
    assert reconciliation is not None
    assert not reconciliation.ready
    assert reconciliation.errors.get("coverage") == "uncovered_pre_journal_work"
    assert runtime.termination_reason is None


async def test_resume_reconciled_refuses_closed_mission() -> None:
    engagement = _engagement("a.local")
    deps = _react_deps(engagement, _TimingGateway())
    await seed_from_engagement(AttackGraphService(deps.graph_store), engagement)
    mission_id = await _mission_world(deps, ["task-c1"])
    saver = build_checkpointer(enabled=True, registry=deps.state_codecs)
    runner = AgentLoopRunner(deps, settings=deps.settings, checkpointer=saver)
    runtime = _runtime(
        "run-closed", str(engagement.engagement_id), "a", "map the in-scope host", mission_id
    )
    # resume_reconciled gates on live mission state first and returns the
    # runtime unchanged when the mission is not active.
    stored = await deps.task_graph_store.get_mission(MissionId(mission_id))
    assert stored is not None
    await deps.task_graph_store.update_mission(
        stored.model_copy(update={"status": MissionStatus.BLOCKED, "revision": stored.revision + 1})
    )
    # Bind first so the refusal below comes from the mission gate alone.
    from redteam.orchestration.coordination import coordinator_for

    await coordinator_for(deps.task_graph_store, deps.path_store).bind(
        "run-closed", mission_id, str(engagement.engagement_id)
    )
    out = await runner.resume_reconciled(runtime)
    assert out == runtime
