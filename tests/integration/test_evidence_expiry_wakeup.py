"""Grounded expiry -> existing lifecycle/driver, with synthetic judgments only."""

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from redteam.app.specialist_run import SpecialistLifecycle
from redteam.core.contracts.events import EventEnvelope, EventType
from redteam.core.enums import AgentPhase, TaskStatus
from redteam.orchestration.evidence_validity import refresh_validity
from redteam.orchestration.instance_driver import InstanceDriver
from redteam.orchestration.llm_requests import RunRequestLedger
from redteam.orchestration.outbox import deliver_pending
from redteam.orchestration.planning_admission import PlanningAdmission
from tests.integration.test_evidence_validity_consumers import bind, measured, measured_world
from tests.integration.test_native_react_loop import ScriptedNativeLLM, _engagement, _native_deps
from tests.integration.test_native_specialist_closed_loop import ClosedLoopProvider, assembled
from tests.integration.test_ready_task_activation import prepared
from tests.unit.test_orchestration_coordination import ENG, NOW, RUN


class Clock(datetime):
    value = datetime.now(timezone.utc)

    @classmethod
    def now(cls, tz=None):
        return cls.value if tz is None else cls.value.astimezone(tz)


class Judgment:
    """No provider or target calls; canonical refresh still uses real stores."""
    def __init__(self, deps, hook=None):
        self.coordinator, self.deps, self.hook = deps.coordinator, deps, hook
        self.calls = []

    async def advance(self, runtime):
        current = await refresh_validity(self.deps, runtime)
        self.calls.append([item.status for item in current])
        if self.hook:
            await self.hook()
        return []


async def expiring(monkeypatch, *, future=True):
    from redteam.app import specialist_run
    from redteam.orchestration import evidence_validity

    world, runtime, path, _ = await measured_world()
    world.deps.specialist_policy = SimpleNamespace()
    Clock.value = datetime.now(timezone.utc)
    monkeypatch.setattr(evidence_validity, "datetime", Clock)
    monkeypatch.setattr(specialist_run, "datetime", Clock)
    deadline = Clock.value + timedelta(seconds=60)
    node = path.steps[0].target_node_ids[0]
    await measured(world, runtime, node, "expiry", deadline.isoformat())
    assert (await bind(world, runtime, node, "expiry", "not_expired")).ok
    loop = SpecialistLifecycle(runtime, world.deps)
    loop.planner = Judgment(world.deps)
    await loop.state("active")
    if not future:
        Clock.value = deadline + timedelta(seconds=1)
    return world, runtime, loop, node, deadline


@pytest.mark.parametrize("resume_overdue", [False, True])
async def test_expiry_refreshes_once_without_rewriting_source_or_repeating_model(
    monkeypatch, resume_overdue,
):
    world, runtime, loop, _, deadline = await expiring(monkeypatch, future=not resume_overdue)
    original = (await world.evidence.get("proof-race")).model_dump()
    task = (await world.tasks.get_task(runtime.active_task_id)).model_dump()
    if not resume_overdue:
        assert await loop.evidence_wakeup() == deadline
        assert loop.planner.calls == []
        Clock.value = deadline + timedelta(seconds=1)
    assert await loop.evidence_wakeup() is None
    assert loop.planner.calls == [["stale"]]
    for _ in range(8):
        assert await loop.evidence_wakeup() is None
    assert loop.planner.calls == [["stale"]]
    assert (await world.evidence.get("proof-race")).model_dump() == original
    assert (await world.tasks.get_task(runtime.active_task_id)).model_dump() == task
    events = world.deps.event_bus.history(event_type=EventType.EVIDENCE_VALIDITY_CHANGED)
    assert [event.payload["status"] for event in events] == ["valid", "stale"]


async def test_changed_condition_rearms_and_duplicate_notification_costs_no_judgment(monkeypatch):
    world, runtime, loop, node, deadline = await expiring(monkeypatch)
    assert await loop.evidence_wakeup() == deadline
    await measured(world, runtime, node, "expiry", (deadline + timedelta(seconds=90)).isoformat())
    await refresh_validity(world.deps, runtime)
    # A later observation cannot erase the old baseline; it is stale, not a
    # magically extended session. Notifications remain advisory.
    assert await loop.evidence_wakeup() is None
    assert loop.planner.calls == [["stale"]]
    assert await loop.evidence_wakeup() is None
    assert len(loop.planner.calls) == 1


async def test_change_during_judgment_is_not_consumed_as_reviewed(monkeypatch):
    world, runtime, loop, node, _ = await expiring(monkeypatch)
    async def mutate():
        await measured(world, runtime, node, "expiry", "2020-01-01T00:00:00+00:00")
        await refresh_validity(world.deps, runtime)
    loop.planner.hook = mutate
    await loop.propose(runtime)
    loop.planner.hook = None
    assert await loop.evidence_wakeup() is None
    assert loop.planner.calls == [["valid"], ["stale"]]


async def test_expiry_with_writer_fence_holds_without_timer_retry_or_fence_release(monkeypatch):
    world, runtime, loop, _, deadline = await expiring(monkeypatch)
    assert await loop.evidence_wakeup() == deadline
    admission = PlanningAdmission(world.deps.coordinator)
    writer = await admission.begin_reference_update(str(runtime.run_id), ["evidence"])
    Clock.value = deadline + timedelta(seconds=1)
    assert await loop.evidence_wakeup() is None
    assert loop.diagnostic.code == "evidence_validity_wakeup_held"
    assert loop.diagnostic.error_type == "StateConflict"
    assert loop.planner.calls == []
    pending = await admission.pending_reference_updates(str(runtime.run_id))
    assert len(pending) == 1 and pending[0].writer == writer


async def test_schedule_read_failure_has_safe_diagnostic_not_an_empty_success(monkeypatch):
    _, _, loop, _, _ = await expiring(monkeypatch)
    async def failed(*args):
        raise OSError("secret private evidence path")
    monkeypatch.setattr("redteam.app.specialist_run.validity_schedule", failed)
    assert await loop.evidence_wakeup() is None
    assert loop.diagnostic.code == "evidence_validity_wakeup_held"
    assert loop.diagnostic.error_type == "OSError"
    assert "secret" not in loop.diagnostic.model_dump_json()
    assert loop.planner.calls == []


async def test_cancel_during_expiry_check_propagates(monkeypatch):
    _, _, loop, _, _ = await expiring(monkeypatch)
    async def cancelled(*args):
        raise asyncio.CancelledError
    monkeypatch.setattr("redteam.app.specialist_run.validity_schedule", cancelled)
    with pytest.raises(asyncio.CancelledError):
        await loop.evidence_wakeup()


@pytest.mark.parametrize("durable", [False, True])
async def test_grounded_expiry_reaches_actual_planner_atomizer_and_shared_request_ledger(
    tmp_path, monkeypatch, durable,
):
    from redteam.core.enums import MissionStatus
    from redteam.orchestration.state import RuntimeState
    from tests.integration import _engine_llm_harness as h

    provider = ClosedLoopProvider()
    engagement, deps, gateway = await assembled(tmp_path, provider, durable=durable)
    mission, template = h.make_mission_task(
        mission_id="expiry-mission", engagement_id=str(engagement.engagement_id),
        criteria=["Original response preserved"],
    )
    mission.status = MissionStatus.ACTIVE
    await deps.task_graph_store.create_mission(mission)
    for identity in ("independent-a", "independent-b"):
        await deps.task_graph_store.create_task(template.model_copy(update={
            "task_id": identity, "assigned_agent": "web-executor",
            "related_attack_graph_node_ids": ["host:a.local"],
        }))
    base = RuntimeState(run_id="expiry-native", mission_id=mission.mission_id,
                        engagement_id=engagement.engagement_id, remaining_budget=10)
    await deps.coordinator.bind(str(base.run_id), str(mission.mission_id),
                                str(engagement.engagement_id))
    ledger = RunRequestLedger(deps.coordinator)
    await ledger.bind(str(base.run_id), deps.settings.llm_request_policy)
    await deps.evidence_store.append(h.make_tool_evidence(
        evidence_id="proof-race", run_id=str(base.run_id),
        engagement_id=str(engagement.engagement_id), action_id="origin", invocation_id="origin",
        path_id="synthetic-original", path_version=1, step_id="observed",
        target_node_id="host:a.local",
    ))
    synthetic_world = SimpleNamespace(graph_store=deps.graph_store, deps=deps)
    deadline = datetime.now(timezone.utc) + timedelta(seconds=0.5)
    await measured(synthetic_world, base, "host:a.local", "expiry", deadline.isoformat())
    assert (await bind(synthetic_world, base, "host:a.local", "expiry", "not_expired")).ok
    loop = SpecialistLifecycle(base, deps)
    await loop.state("active")
    release, started = asyncio.Event(), asyncio.Event()
    class Runner:
        async def run(self, runtime):
            if runtime.active_task_id in {"independent-a", "independent-b"}:
                started.set()
                await release.wait()
            return runtime.model_copy(update={"phase": AgentPhase.FINISHED})

        def request_stop(self):
            release.set()

    try:
        async with InstanceDriver(deps, checkpointer=object()) as driver:
            await driver.own_run(str(base.run_id))
            monkeypatch.setattr(driver, "runner_for", lambda _: Runner())
            stream = driver.drive_ready_tasks(
                base, max_instances=3, max_activations=3, on_wakeup=loop.evidence_wakeup,
                wake_events=(EventType.EVIDENCE_VALIDITY_CHANGED,),
            )
            first = asyncio.create_task(anext(stream))
            await asyncio.wait_for(started.wait(), 1)
            event = await asyncio.wait_for(first, 4)
            assert event.runtime.active_task_id not in {"independent-a", "independent-b"}
            assert not release.is_set()
            assert len(provider.planning_inputs) == 1
            assert provider.planning_inputs[0]["evidence_validity"][0]["status"] == "stale"
            accounting = await ledger.snapshot(str(base.run_id))
            assert accounting["spent"] == len(provider.requests) == 2
            assert accounting["reserved"] == 0
            created = await deps.task_graph_store.get_task(event.runtime.active_task_id)
            assert created.completion_contract_version == 2 and created.completion_criteria
            assert not gateway.invocations  # actor bodies above are scheduling fakes
            assert loop.diagnostic is None
            release.set()
            assert len([event async for event in stream]) == 2
    finally:
        release.set()


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
async def test_driver_deadline_starts_followup_while_independent_workers_are_busy(
    tmp_path, monkeypatch, backend,
):
    w, _, base = await prepared(tmp_path, backend)
    deps = replace(_native_deps(_engagement(), ScriptedNativeLLM([])),
                   task_graph_store=w.tasks, path_store=w.paths, coordinator=w.coordinator,
                   event_bus=w.bus)
    release, started = asyncio.Event(), asyncio.Event()
    calls, scheduled_deadline = [], datetime.now(timezone.utc) + timedelta(seconds=0.1)

    async def followup():
        if datetime.now(timezone.utc) < scheduled_deadline:
            return scheduled_deadline
        if calls:
            return None
        calls.append("expired")
        async def admit(tx):
            tx.admit(RUN, str(base.mission_id))
            task = await tx.tasks.get_task("task-a")
            await tx.tasks.create_task(task.model_copy(update={
                "task_id": "followup", "status": TaskStatus.READY, "occupant_owner": None,
            }))
            tx.queue(EventEnvelope(
                event_id="expiry-followup", event_type=EventType.TASK_CREATED,
                run_id=RUN, engagement_id=ENG, source="synthetic-planner", timestamp=NOW,
            ))
        await w.coordinator.change(admit)
        await deliver_pending(w.coordinator, run_id=RUN, engagement_id=ENG, event_bus=w.bus)
        return None

    class Runner:
        async def run(self, runtime):
            if runtime.active_task_id != "followup":
                started.set()
                await release.wait()
            return runtime.model_copy(update={"phase": AgentPhase.FINISHED})

        def request_stop(self):
            release.set()

    async with InstanceDriver(deps, checkpointer=object()) as driver:
        monkeypatch.setattr(driver, "runner_for", lambda _: Runner())
        stream = driver.drive_ready_tasks(base, max_instances=3, max_activations=3,
                                          on_wakeup=followup)
        next_event = asyncio.create_task(anext(stream))
        await asyncio.wait_for(started.wait(), 1)
        event = await asyncio.wait_for(next_event, 2)
        assert event.runtime.active_task_id == "followup"
        assert not release.is_set() and calls == ["expired"]
        release.set()
        assert len([event async for event in stream]) == 2


@pytest.mark.parametrize("blocked", ["empty", "partial"])
async def test_driver_does_not_invoke_deadline_callback_for_quiescent_or_partial_selection(
    tmp_path, monkeypatch, blocked,
):
    w, _, base = await prepared(tmp_path)
    deps = replace(_native_deps(_engagement(), ScriptedNativeLLM([])),
                   task_graph_store=w.tasks, path_store=w.paths, coordinator=w.coordinator)
    calls = []
    async def wakeup():
        calls.append("should not run")
        return datetime.now(timezone.utc)
    if blocked == "empty":
        for task in await w.tasks.list_tasks(mission_id=base.mission_id):
            await w.tasks.transition_task(task.task_id, TaskStatus.CANCELLED, now=NOW)
    async with InstanceDriver(deps, checkpointer=object()) as driver:
        events = [event async for event in driver.drive_ready_tasks(
            base, max_instances=1, max_activations=1, allow_new_tasks=blocked != "partial",
            on_wakeup=wakeup,
        )]
    assert not events and not calls
