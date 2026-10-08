"""Real stores/loop/outbox, grounded timers; no model or target calls."""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from redteam.core.contracts.mission import MissionPlan, Provenance
from redteam.core.enums import AgentPhase, MissionStatus
from redteam.core.ids import MissionId
from redteam.orchestration.coordination import coordinator_for
from redteam.orchestration.delegated_runtime import _worker_hold_fingerprint, decide_delegations
from redteam.orchestration.delegation import DelegationState
from redteam.orchestration.delegation_decision import TERMINATION_WORKER_HELD
from redteam.orchestration.engine import InvestigationEngine
from redteam.orchestration.evidence_expiry import EXPIRY_EVENT, EvidenceExpiryWaiter
from redteam.orchestration.evidence_validity import assess, current_validity
from redteam.orchestration.nodes.replan import replan_node
from redteam.orchestration.paths import PathPlan, SQLitePathStore
from redteam.orchestration.state import TerminationReason, initial_graph_state
from redteam.orchestration.task_graph import SQLiteTaskGraphStore
from tests.integration import _engine_llm_harness as h
from tests.unit.test_coordination_sqlite import records
from tests.unit.test_specialist_p2_artifacts import change_fact, world
from tests.unit.test_specialist_profile import _atomized


async def timer_world(tmp_path, *, sqlite=False, seconds=0.2):
    seeded, store, original, _, context = await world(tmp_path)
    deps = h.make_world(context.deps.llm_client, graph_store=seeded.store).deps
    deps.evidence_store = store
    deps.condition_waiter = EvidenceExpiryWaiter(2)
    if sqlite:
        database = tmp_path / "state.sqlite3"
        deps.task_graph_store = SQLiteTaskGraphStore(database)
        deps.path_store = SQLitePathStore(database)
    deps.coordinator = coordinator_for(deps.task_graph_store, deps.path_store)
    runtime = context.runtime.model_copy(update={
        "mission_id": MissionId("mission"), "active_path_id": "path", "active_path_version": 1,
        "phase": AgentPhase.REPLAN, "termination_reason": TerminationReason.INVESTIGATION_HELD,
    })
    now = datetime.now(timezone.utc)
    await deps.task_graph_store.create_mission(MissionPlan(
        mission_id=runtime.mission_id, engagement_id=runtime.engagement_id, objective="Inspect",
        success_criteria=["Original evidence"], status=MissionStatus.ACTIVE,
        created_at=now, updated_at=now, provenance=Provenance(source="operator"),
    ))
    await deps.coordinator.bind(str(runtime.run_id), str(runtime.mission_id),
                                str(runtime.engagement_id))
    path = PathPlan.model_validate({
        **_atomized(seeded.node_id)["path"], "run_id": str(runtime.run_id), "path_id": "path",
        "version": 1, "status": "active", "goal": "Inspect",
    })
    await deps.path_store.register(path)
    state = DelegationState(
        delegation_id="d", agent_id="web-api-agent", objective="Inspect", path_id="path",
        path_version=1, path_step_id="s1", budget={"max_actions": 2, "max_seconds": 10},
        decision_limit=8, started_at=now, evidence_ids=["original"], status="held",
        termination_reason=TERMINATION_WORKER_HELD,
    )
    runtime.delegations[state.delegation_id] = state
    await change_fact(seeded, store, context, key="expiry_at",
                      value=(now + timedelta(seconds=seconds)).isoformat())
    bound = await assess({"evidence_id": "original", "conditions": [{
        "node_id": seeded.node_id, "container": "fact", "key": "expiry_at",
        "test": "not_expired",
    }]}, context)
    assert bound.ok
    engine = InvestigationEngine(deps)
    state.held_fingerprint = await _worker_hold_fingerprint(engine, runtime, state)
    assert state.held_fingerprint
    return seeded, store, original, deps, runtime, context


@pytest.mark.parametrize("sqlite", [False, True])
async def test_grounded_expiry_wakes_replan_and_only_related_claim(tmp_path, sqlite):
    seeded, store, original, deps, runtime, _ = await timer_world(tmp_path, sqlite=sqlite)
    before = runtime.model_dump()
    updated = (await replan_node(initial_graph_state(runtime), deps=deps))["runtime"]
    assert updated.phase == AgentPhase.OBSERVE and updated.termination_reason is None
    assert updated.delegations["d"].model_dump() == before["delegations"]["d"]
    assert updated.remaining_budget == runtime.remaining_budget
    assert updated.model_requests_used == 0 and not deps.llm_client.calls
    assert await store.get("original") == original
    assert (await current_validity(store, seeded.service, str(runtime.run_id),
                                   "original"))["status"] == "stale"
    assert (await current_validity(store, seeded.service, str(runtime.run_id),
                                   "later-expiry_at"))["status"] == "unbound"
    ledger = await records(deps.coordinator)
    assert sum(k == "validity_timer" for k, _ in ledger) == 1
    assert sum(k == "outbox" and row["envelope"]["event_type"] == EXPIRY_EVENT
               for (k, _), row in ledger.items()) == 1
    # Recovery reuses the event until the old held checkpoint advances.
    assert await deps.condition_waiter.wait_for_change(runtime, deps)
    assert await records(deps.coordinator) == ledger
    engine = InvestigationEngine(deps)
    runtime.delegations["d"].held_fingerprint = await _worker_hold_fingerprint(
        engine, runtime, runtime.delegations["d"],
    )
    assert not await deps.condition_waiter.wait_for_change(runtime, deps)


async def test_sqlite_deadline_and_event_survive_new_instances(tmp_path):
    _, _, _, deps, runtime, _ = await timer_world(tmp_path, sqlite=True)
    assert await deps.condition_waiter.wait_for_change(runtime, deps)
    before = await records(deps.coordinator)
    database = tmp_path / "state.sqlite3"
    deps.task_graph_store = SQLiteTaskGraphStore(database)
    deps.path_store = SQLitePathStore(database)
    deps.coordinator = coordinator_for(deps.task_graph_store, deps.path_store)
    assert await EvidenceExpiryWaiter(2).wait_for_change(runtime, deps)
    assert await records(deps.coordinator) == before


@pytest.mark.parametrize("case", ["unrelated", "limit", "time", "decision", "closed", "steps"])
async def test_no_wait_or_inference_outside_related_live_budget(tmp_path, case):
    _, _, _, deps, runtime, _ = await timer_world(tmp_path, seconds=1)
    state = runtime.delegations["d"]
    if case == "unrelated":
        state.evidence_ids = ["unrelated"]
    elif case == "limit":
        deps.condition_waiter = EvidenceExpiryWaiter(0.01)
    elif case == "time":
        state.budget.max_seconds = 0.01
    elif case == "decision":
        state.decision_attempts_used = state.decision_limit
    elif case == "closed":
        async def close(change):
            change.binding(str(runtime.run_id), str(runtime.mission_id))["closed"] = True
        await deps.coordinator.change(close)
    else:
        runtime.step_count = deps.settings.max_steps
    updated = (await replan_node(initial_graph_state(runtime), deps=deps))["runtime"]
    assert updated.phase == AgentPhase.FINISHED
    assert not deps.llm_client.calls
    assert not any(k == "outbox" and row["envelope"]["event_type"] == EXPIRY_EVENT
                   for (k, _), row in (await records(deps.coordinator)).items())


async def test_cancelled_wait_has_durable_reservation_and_no_event(tmp_path):
    _, _, _, deps, runtime, _ = await timer_world(tmp_path, seconds=1)
    task = asyncio.create_task(deps.condition_waiter.wait_for_change(runtime, deps))
    await asyncio.sleep(0.03)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    ledger = await records(deps.coordinator)
    assert ledger[("validity_wait_used", str(runtime.run_id))] > 0
    assert not any(k == "outbox" for k, _ in ledger)


async def test_changed_source_abandons_timer_without_rewriting_evidence(tmp_path):
    seeded, store, original, deps, runtime, context = await timer_world(tmp_path, seconds=0.5)
    task = asyncio.create_task(deps.condition_waiter.wait_for_change(runtime, deps))
    await asyncio.sleep(0.03)
    await change_fact(seeded, store, context, key="expiry_at",
                      value=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat())
    assert not await task
    assert await store.get("original") == original
    assert not any(k == "outbox" for k, _ in (await records(deps.coordinator)))


async def test_pending_operator_stop_interrupts_wait_without_inference(tmp_path):
    _, _, _, deps, runtime, _ = await timer_world(tmp_path, seconds=1)
    task = asyncio.create_task(deps.condition_waiter.wait_for_change(runtime, deps))
    await asyncio.sleep(0.03)

    async def request_stop(change):
        change.records[("intent", "stop")] = {
            "run_id": str(runtime.run_id), "status": "received",
            "intent": {"mission_id": str(runtime.mission_id), "intent_type": "stop"},
        }

    await deps.coordinator.change(request_stop)
    assert await asyncio.wait_for(task, 0.5)  # Return to the existing OBSERVE intent handler.
    assert not deps.llm_client.calls
    assert not any(k == "outbox" for k, _ in (await records(deps.coordinator)))


async def test_existing_delegation_consumer_rechecks_after_timer_once(tmp_path):
    seeded, _, _, deps, runtime, _ = await timer_world(tmp_path)

    class HeldModel:
        calls: list = []

        async def complete_structured(self, **kwargs):
            self.calls.append(kwargs)
            return {"decision": "hold", "reason": "Need fresh original observation",
                    "unresolved": ["Reobserve expired condition"]}

    deps.llm_client = HeldModel()
    updated = (await replan_node(initial_graph_state(runtime), deps=deps))["runtime"]
    engine = InvestigationEngine(deps)
    engine.updated_runtime = engine._event_runtime = updated
    context = await engine.context(updated, await seeded.service.observe(step=0))
    path = await deps.path_store.get_active(str(runtime.run_id))
    before = updated.delegations["d"].decision_attempts_used
    await decide_delegations(engine, path, updated, context)
    assert len(deps.llm_client.calls) == 1
    updated = engine.updated_runtime
    assert updated.delegations["d"].decision_attempts_used == before + 1
    assert updated.delegations["d"].status == "held"
    assert not await deps.condition_waiter.wait_for_change(updated, deps)
    await decide_delegations(engine, path, updated, context)
    assert len(deps.llm_client.calls) == 1


async def test_expired_allowance_of_one_delegation_cannot_starve_live_sibling(tmp_path):
    _, _, _, deps, runtime, _ = await timer_world(tmp_path)
    runtime.delegations["live"] = runtime.delegations["d"].model_copy(deep=True,
        update={"delegation_id": "live"})
    runtime.delegations["d"].budget.max_seconds = 0.01
    assert await deps.condition_waiter.wait_for_change(runtime, deps)
    events = [row["envelope"] for (kind, _), row in (await records(deps.coordinator)).items()
              if kind == "outbox"]
    assert len(events) == 1 and events[0]["payload"]["delegation_id"] == "live"
