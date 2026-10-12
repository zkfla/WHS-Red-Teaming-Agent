"""F02 integration: expiry-only bounded re-judgment over real SQLite + fakes."""

# Explicit provider doubles and temp SQLite only; no live target or paid LLM.
# A controllable clock advances past an observed valid_until with no new write
# event, and the real consumer wiring (outbox sequence + bus delivery) is asserted.

import asyncio
from datetime import datetime, timedelta, timezone

import redteam.app.central_run as central_run_module
from redteam.app.central_run import recheck_evidence_expiry
from redteam.attack_graph.predicates import Confidence
from redteam.attack_graph.predicates import Provenance as GraphProvenance
from redteam.attack_graph.service import AttackGraphService
from redteam.core.contracts.events import EventType
from redteam.core.contracts.mission import OperatorIntent, Provenance
from redteam.core.enums import GraphNodeType, MissionStatus
from redteam.core.ids import GraphNodeId
from redteam.orchestration.central_runtime import process_controls
from redteam.orchestration.coordination import StateConflict
from redteam.orchestration.evidence_validity import ExpiryWatch, assess
from redteam.orchestration.local_tools import LocalToolContext
from redteam.orchestration.operator_intents.models import IntentRecord
from redteam.orchestration.specialist_planner import SpecialistPlanner
from tests.integration.test_central_completion import selection
from tests.integration.test_central_development import TARGET
from tests.integration.test_central_recovery import start
from tests.integration.test_plan4a_completion import (
    Clock,
    bind,
    observed_tools,
    originals_for,
)


def _freeze(monkeypatch, moment):
    Clock.value = moment
    monkeypatch.setattr("redteam.orchestration.evidence_validity.datetime", Clock)
    monkeypatch.setattr("redteam.app.central_run.datetime", Clock)


async def _validity_sequence(state):
    return await state.transaction(
        lambda tx: tx.latest_event_sequence((EventType.EVIDENCE_VALIDITY_CHANGED,)),
        write=False,
    )


async def test_expiry_only_triggers_one_bounded_rejudgment(tmp_path, monkeypatch):
    moment = datetime.now(timezone.utc)
    _freeze(monkeypatch, moment)
    async with selection(tmp_path, tools=observed_tools()) as (session, llm):
        runtime, = await start(session, llm)
        originals = await originals_for(session, runtime)
        await bind(session, runtime, originals, expiry=True)
        state = session.deps.central_state
        received = []
        await session.deps.event_bus.subscribe(
            EventType.EVIDENCE_VALIDITY_CHANGED, _collector(received))
        watch = ExpiryWatch()
        changes, upcoming = await recheck_evidence_expiry(
            session.deps, runtime, watch, baseline_silent=True)
        assert changes == []
        assert upcoming is not None
        seq_before = await _validity_sequence(state)
        records_before = await session.deps.evidence_store.list_for_run(runtime.run_id)
        tasks_before = await state.list_tasks()
        llm_before = len(llm.requests)
        Clock.value = moment + timedelta(seconds=6)
        changes, upcoming = await recheck_evidence_expiry(session.deps, runtime, watch)
        assert len(changes) == 1
        assert (changes[0].old_status, changes[0].new_status) == ("valid", "stale")
        assert "observed expiry" in changes[0].reason
        assert changes[0].evidence_refs[0] == originals[0]
        assert upcoming is None
        seq_after = await _validity_sequence(state)
        assert seq_after > seq_before
        assert len(received) == 1
        assert received[0].payload["evidence_refs"] == changes[0].evidence_refs
        assert received[0].payload["reason"] == changes[0].reason
        assert len(llm.requests) == llm_before
        records_after = await session.deps.evidence_store.list_for_run(runtime.run_id)
        assert {str(record.evidence_id) for record in records_after} == {
            str(record.evidence_id) for record in records_before}
        assert [task.status for task in await state.list_tasks()] == [
            task.status for task in tasks_before]
        again, _ = await recheck_evidence_expiry(session.deps, runtime, watch)
        assert again == []
        assert await _validity_sequence(state) == seq_after
        assert len(received) == 1


async def test_no_invented_ttl_without_observed_deadline(tmp_path, monkeypatch):
    moment = datetime.now(timezone.utc)
    _freeze(monkeypatch, moment)
    async with selection(tmp_path, tools=observed_tools()) as (session, llm):
        runtime, = await start(session, llm)
        originals = await originals_for(session, runtime)
        await bind(session, runtime, originals, expiry=False)
        state = session.deps.central_state
        received = []
        await session.deps.event_bus.subscribe(
            EventType.EVIDENCE_VALIDITY_CHANGED, _collector(received))
        watch = ExpiryWatch()
        changes, upcoming = await recheck_evidence_expiry(
            session.deps, runtime, watch, baseline_silent=True)
        assert changes == []
        assert upcoming is None
        Clock.value = moment + timedelta(hours=1)
        changes, upcoming = await recheck_evidence_expiry(session.deps, runtime, watch)
        assert changes == []
        assert upcoming is None
        assert await _validity_sequence(state) == 0
        assert received == []


async def test_pause_blocks_expiry_emit_without_new_calls(tmp_path, monkeypatch):
    moment = datetime.now(timezone.utc)
    _freeze(monkeypatch, moment)
    async with selection(tmp_path, tools=observed_tools()) as (session, llm):
        runtime, = await start(session, llm)
        originals = await originals_for(session, runtime)
        await bind(session, runtime, originals, expiry=True)
        state = session.deps.central_state
        received = []
        await session.deps.event_bus.subscribe(
            EventType.EVIDENCE_VALIDITY_CHANGED, _collector(received))
        watch = ExpiryWatch()
        await recheck_evidence_expiry(
            session.deps, runtime, watch, baseline_silent=True)
        await state.append(IntentRecord(
            intent=OperatorIntent(
                intent_id="operator-intent", mission_id=session.runtime.mission_id,
                intent_type="pause", instruction="pause",
                created_at=datetime.now(timezone.utc),
                provenance=Provenance(source="fixture:operator"),
            ), run_id=session.runtime.run_id, correlation_id="operator-intent",
        ))
        await process_controls(session.deps)
        mission = await state.get_mission(session.runtime.mission_id)
        assert mission.status is MissionStatus.PAUSED
        llm_before = len(llm.requests)
        Clock.value = moment + timedelta(seconds=6)
        changes, _ = await recheck_evidence_expiry(session.deps, runtime, watch)
        assert changes == []
        assert await _validity_sequence(state) == 0
        assert received == []
        assert len(llm.requests) == llm_before


async def test_restart_recovery_emits_no_duplicates(tmp_path, monkeypatch):
    moment = datetime.now(timezone.utc)
    _freeze(monkeypatch, moment)
    async with selection(tmp_path, tools=observed_tools()) as (session, llm):
        runtime, = await start(session, llm)
        originals = await originals_for(session, runtime)
        await bind(session, runtime, originals, expiry=True)
        state = session.deps.central_state
        received = []
        await session.deps.event_bus.subscribe(
            EventType.EVIDENCE_VALIDITY_CHANGED, _collector(received))
        watch = ExpiryWatch()
        await recheck_evidence_expiry(
            session.deps, runtime, watch, baseline_silent=True)
        seq_before = await _validity_sequence(state)
        Clock.value = moment + timedelta(seconds=6)
        changes, _ = await recheck_evidence_expiry(session.deps, runtime, watch)
        assert len(changes) == 1
        seq_after = await _validity_sequence(state)
        assert seq_after > seq_before
        restarted = ExpiryWatch()
        changes, _ = await recheck_evidence_expiry(
            session.deps, runtime, restarted, baseline_silent=True)
        assert changes == []
        assert await _validity_sequence(state) == seq_after
        await state.deliver_outbox(session.deps.event_bus.bus)
        assert len(received) == 1
        assert await _validity_sequence(state) == seq_after


async def _bind_deadline(session, runtime, originals, key, seconds):
    graph = AttackGraphService(session.deps.graph_store)
    await graph.upsert_node(
        GraphNodeType.HOST, node_id=GraphNodeId(TARGET),
        facts={key: (Clock.value + timedelta(seconds=seconds)).isoformat()},
        provenance=GraphProvenance(run_id=str(runtime.run_id),
                                   evidence_id=originals[0],
                                   confidence=Confidence.MEASURED),
    )
    result = await assess(
        {"evidence_id": originals[0],
         "conditions": [{"node_id": TARGET, "container": "fact",
                           "key": key, "test": "not_expired"}]},
        LocalToolContext(session.deps, runtime))
    assert result.ok, result.error


async def _wait_for_validity(state, timeout=25.0):
    started = asyncio.get_running_loop().time()
    while True:
        sequence = await _validity_sequence(state)
        if sequence > 0:
            return sequence
        if asyncio.get_running_loop().time() - started > timeout:
            raise TimeoutError("validity notification never arrived")
        await asyncio.sleep(0.05)


async def _wait_for_delivery(received, timeout=25.0):
    started = asyncio.get_running_loop().time()
    while not received:
        if asyncio.get_running_loop().time() - started > timeout:
            raise TimeoutError("validity notification never delivered")
        await asyncio.sleep(0.05)
    return received


def _collector(received):
    async def collect(envelope):
        received.append(envelope)
    return collect


async def test_failed_queue_retries_without_dropping_change(tmp_path, monkeypatch):
    moment = datetime.now(timezone.utc)
    _freeze(monkeypatch, moment)
    async with selection(tmp_path, tools=observed_tools()) as (session, llm):
        runtime, = await start(session, llm)
        originals = await originals_for(session, runtime)
        await bind(session, runtime, originals, expiry=True)
        state = session.deps.central_state
        received = []
        await session.deps.event_bus.subscribe(
            EventType.EVIDENCE_VALIDITY_CHANGED, _collector(received))
        watch = ExpiryWatch()
        await recheck_evidence_expiry(
            session.deps, runtime, watch, baseline_silent=True)
        seq_before = await _validity_sequence(state)
        real_queue = central_run_module.queue_validity_change
        calls = []

        def flaky(tx, envelope):
            calls.append(1)
            if len(calls) == 1:
                raise StateConflict("boom")
            return real_queue(tx, envelope)

        monkeypatch.setattr(central_run_module, "queue_validity_change", flaky)
        Clock.value = moment + timedelta(seconds=6)
        try:
            await recheck_evidence_expiry(session.deps, runtime, watch)
        except StateConflict:
            pass
        else:
            raise AssertionError("queue failure swallowed")
        changes, _ = await recheck_evidence_expiry(session.deps, runtime, watch)
        assert len(changes) == 1
        assert (changes[0].old_status, changes[0].new_status) == ("valid", "stale")
        assert await _validity_sequence(state) > seq_before
        assert len(received) == 1


async def test_run_discovers_late_binding_without_prior_ttl(tmp_path, monkeypatch):
    moment = datetime.now(timezone.utc)
    _freeze(monkeypatch, moment)
    async with selection(tmp_path, tools=observed_tools()) as (session, llm):
        runtime, = await start(session, llm)
        originals = await originals_for(session, runtime)
        await bind(session, runtime, originals, expiry=False)
        state = session.deps.central_state
        received = []
        await session.deps.event_bus.subscribe(
            EventType.EVIDENCE_VALIDITY_CHANGED, _collector(received))
        # Slow (but completing) planner advances keep the drive loop and its
        # ticks alive while a late binding is introduced after the baseline.
        real_advance = SpecialistPlanner.advance

        async def slow_advance(self, run):
            await asyncio.sleep(3)
            return await real_advance(self, run)

        monkeypatch.setattr(SpecialistPlanner, "advance", slow_advance)
        run_task = asyncio.create_task(session.run())
        try:
            await asyncio.sleep(0.3)
            await _bind_deadline(session, runtime, originals, "observed_late", 5)
            await asyncio.sleep(0.5)
            Clock.value = moment + timedelta(seconds=6)
            await asyncio.wait_for(_wait_for_validity(state), timeout=25)
            await asyncio.wait_for(_wait_for_delivery(received), timeout=25)
            assert len(received) == 1
            assert received[0].payload["evidence_refs"][0] == originals[0]
            assert (received[0].payload["old_status"],
                    received[0].payload["new_status"]) == ("valid", "stale")
        finally:
            try:
                await asyncio.wait_for(run_task, timeout=60)
            except (asyncio.TimeoutError, TimeoutError):
                run_task.cancel()
                await asyncio.gather(run_task, return_exceptions=True)


async def test_run_recomputes_earlier_deadline_over_far_future(tmp_path, monkeypatch):
    moment = datetime.now(timezone.utc)
    _freeze(monkeypatch, moment)
    async with selection(tmp_path, tools=observed_tools()) as (session, llm):
        runtime, = await start(session, llm)
        originals = await originals_for(session, runtime)
        await _bind_deadline(session, runtime, originals, "observed_far", 3600)
        state = session.deps.central_state
        received = []
        await session.deps.event_bus.subscribe(
            EventType.EVIDENCE_VALIDITY_CHANGED, _collector(received))
        # Slow (but completing) planner advances keep the drive loop and its
        # ticks alive while a late binding is introduced after the baseline.
        real_advance = SpecialistPlanner.advance

        async def slow_advance(self, run):
            await asyncio.sleep(3)
            return await real_advance(self, run)

        monkeypatch.setattr(SpecialistPlanner, "advance", slow_advance)
        run_task = asyncio.create_task(session.run())
        try:
            await asyncio.sleep(0.3)
            await _bind_deadline(session, runtime, originals, "observed_near", 5)
            await asyncio.sleep(0.5)
            Clock.value = moment + timedelta(seconds=6)
            await asyncio.wait_for(_wait_for_validity(state), timeout=25)
            await asyncio.wait_for(_wait_for_delivery(received), timeout=25)
            assert len(received) == 1
            assert (received[0].payload["old_status"],
                    received[0].payload["new_status"]) == ("valid", "stale")
            expected = (moment + timedelta(seconds=5)).isoformat()
            assert received[0].payload["valid_until"] == expected
        finally:
            try:
                await asyncio.wait_for(run_task, timeout=60)
            except (asyncio.TimeoutError, TimeoutError):
                run_task.cancel()
                await asyncio.gather(run_task, return_exceptions=True)


async def test_recheck_discovers_binding_added_after_baseline(tmp_path, monkeypatch):
    moment = datetime.now(timezone.utc)
    _freeze(monkeypatch, moment)
    async with selection(tmp_path, tools=observed_tools()) as (session, llm):
        runtime, = await start(session, llm)
        originals = await originals_for(session, runtime)
        state = session.deps.central_state
        received = []
        await session.deps.event_bus.subscribe(
            EventType.EVIDENCE_VALIDITY_CHANGED, _collector(received))
        watch = ExpiryWatch()
        changes, upcoming = await recheck_evidence_expiry(
            session.deps, runtime, watch, baseline_silent=True)
        assert changes == []
        assert upcoming is None
        seq_before = await _validity_sequence(state)
        await _bind_deadline(session, runtime, originals, "observed_late", 5)
        changes, upcoming = await recheck_evidence_expiry(session.deps, runtime, watch)
        assert changes == []
        expected = (moment + timedelta(seconds=5)).isoformat()
        assert upcoming is not None and upcoming.isoformat() == expected
        assert await _validity_sequence(state) == seq_before
        Clock.value = moment + timedelta(seconds=6)
        changes, upcoming = await recheck_evidence_expiry(session.deps, runtime, watch)
        assert len(changes) == 1
        assert (changes[0].old_status, changes[0].new_status) == ("valid", "stale")
        assert changes[0].valid_until.isoformat() == expected
        assert upcoming is None
        assert await _validity_sequence(state) > seq_before
        assert len(received) == 1


async def test_failed_delivery_keeps_durable_row_for_redelivery(tmp_path, monkeypatch):
    moment = datetime.now(timezone.utc)
    _freeze(monkeypatch, moment)
    async with selection(tmp_path, tools=observed_tools()) as (session, llm):
        runtime, = await start(session, llm)
        originals = await originals_for(session, runtime)
        await bind(session, runtime, originals, expiry=True)
        state = session.deps.central_state
        received = []
        await session.deps.event_bus.subscribe(
            EventType.EVIDENCE_VALIDITY_CHANGED, _collector(received))
        watch = ExpiryWatch()
        await recheck_evidence_expiry(
            session.deps, runtime, watch, baseline_silent=True)
        real_deliver = state.deliver_outbox
        calls = []

        async def flaky_deliver(event_bus, *, budget_seconds=2.0):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("bus down")
            return await real_deliver(event_bus, budget_seconds=budget_seconds)

        monkeypatch.setattr(state, "deliver_outbox", flaky_deliver)
        Clock.value = moment + timedelta(seconds=6)
        changes, _ = await recheck_evidence_expiry(session.deps, runtime, watch)
        assert len(changes) == 1
        assert len(received) == 0
        again, _ = await recheck_evidence_expiry(session.deps, runtime, watch)
        assert again == []
        await state.deliver_outbox(session.deps.event_bus.bus)
        assert len(received) == 1
