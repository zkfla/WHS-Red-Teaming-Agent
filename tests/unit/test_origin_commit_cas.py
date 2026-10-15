"""TASK-06-C commit-level compare-and-set against a real CentralStateStore.

commit_result reads the origin Task row inside the central transaction. A row that
changed after its original was proven refuses the Task write, with no completion
and no change to the consuming Task row.
"""

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from redteam.core.contracts.engagement import EngagementScope
from redteam.core.contracts.mission import MissionPlan, MissionTask
from redteam.orchestration import central_results
from redteam.orchestration.central_state import CentralStateStore
from redteam.orchestration.completion import OriginProof, origin_row_hash
from redteam.orchestration.coordination import StateConflict, content_hash

NOW = datetime(2026, 10, 11, tzinfo=timezone.utc)
RUN, ENG, MISSION = "run-cas", "eng-cas", "mission-cas"
BINDING = "c" * 64
ORIGIN, CONSUMER = "task-origin", "task-consumer"


def _mission() -> MissionPlan:
    return MissionPlan.model_validate({
        "mission_id": MISSION,
        "engagement_id": ENG,
        "objective": "Investigate the session behaviour",
        "success_criteria": ["Evaluate the session behaviour"],
        "status": "active",
        "created_at": NOW,
        "updated_at": NOW,
        "provenance": {"source": "test"},
    })


def _task(identity: str, objective: str) -> MissionTask:
    return MissionTask.model_validate({
        "task_id": identity,
        "mission_id": MISSION,
        "title": identity,
        "objective": objective,
        "status": "ready",
        "created_at": NOW,
        "updated_at": NOW,
        "provenance": {"source": "test"},
    })


class _NoEvidence:
    """Evidence is not under test here: no original is cited, so no record is read."""

    async def verify_run_integrity(self, run_id):
        return []

    async def get(self, evidence_id):
        return None


class _Bus:
    async def flush(self):
        return None


async def _world(tmp_path):
    state = CentralStateStore(tmp_path / "state.db", run_id=RUN, engagement_id=ENG)
    await state.setup()
    await state.create_mission(_mission())
    await state.bind_run(MISSION, scope=EngagementScope(), binding_hash=BINDING)
    await state.register_task("register-origin", _task(ORIGIN, "confirm the session behaviour"))
    await state.register_task("register-consumer", _task(CONSUMER, "judge the admin path"))
    return state


async def _token(state):
    def read(tx):
        current = tx.admit()
        return current.revision, content_hash(current)

    return await state.transaction(read, write=False)


@pytest.mark.asyncio
async def test_origin_row_change_before_commit_refuses_task_write(tmp_path):
    state = await _world(tmp_path)
    origin_before = await state.get_task(ORIGIN)
    proof = OriginProof(
        "ev-cas", origin_task_id=ORIGIN, origin_task_hash=origin_row_hash(origin_before),
    )
    # The origin row changes through the real owned-update path after it was proven.
    claimed = await state.claim_task(ORIGIN, owner="worker-origin", instance_id="instance-origin")
    await state.transaction(lambda tx: tx.entering(
        ORIGIN, owner="worker-origin", version=claimed.ownership_version,
        instance_id="instance-origin",
    ))
    current_origin = await state.get_task(ORIGIN)
    changed = current_origin.model_copy(update={"objective": "a different objective"})
    await state.transaction(lambda tx: tx.update_task(
        changed, owner="worker-origin", version=claimed.ownership_version,
        expected_hash=content_hash(current_origin),
    ))
    assert origin_row_hash(await state.get_task(ORIGIN)) != proof.origin_task_hash

    consumer = await state.get_task(CONSUMER)
    before = content_hash(consumer)
    result = central_results.result_for(
        consumer, outcome="inconclusive", summary="cas", evidence_ids=[],
    )
    deps = SimpleNamespace(central_state=state, evidence_store=_NoEvidence(), event_bus=_Bus())
    runtime = SimpleNamespace(
        run_id=RUN, engagement_id=ENG, work_owner="owner-consumer", task_ownership_version=1,
    )
    with pytest.raises(
        central_results.OriginTaskChangedConflict,
        match="origin_task_changed_reassessment_required",
    ) as rejected:
        await central_results.commit_result(
            deps, runtime, consumer, result, await _token(state), final=True,
            origin_proofs=(proof,),
        )
    assert isinstance(rejected.value, StateConflict)
    assert rejected.value.origin_task_id == ORIGIN
    assert rejected.value.diagnostic.code == "origin_task_changed_reassessment_required"
    assert rejected.value.diagnostic.cause_scope == "evidence_validity"
    assert rejected.value.diagnostic.owner == "orchestration.central_results"
    assert rejected.value.diagnostic.evidence_ids == ["ev-cas"]
    assert content_hash(await state.get_task(CONSUMER)) == before


@pytest.mark.asyncio
async def test_origin_change_inside_final_transaction_rolls_back_with_diagnostic(
    tmp_path, monkeypatch,
):
    state = await _world(tmp_path)
    origin_before = await state.get_task(ORIGIN)
    proof = OriginProof(
        "ev-cas-final", origin_task_id=ORIGIN,
        origin_task_hash=origin_row_hash(origin_before),
    )
    consumer = await state.get_task(CONSUMER)
    before_consumer = content_hash(consumer)
    before_origin = content_hash(origin_before)
    result = central_results.result_for(
        consumer, outcome="inconclusive", summary="cas", evidence_ids=[],
    )
    deps = SimpleNamespace(central_state=state, evidence_store=_NoEvidence(), event_bus=_Bus())
    runtime = SimpleNamespace(
        run_id=RUN, engagement_id=ENG, work_owner="owner-consumer", task_ownership_version=1,
    )
    token = await _token(state)
    original_transaction = state.transaction

    async def mutate_origin_in_final_transaction(callback, *, write=True):
        def with_changed_source(tx):
            current = tx.get_task(ORIGIN)
            tx._write_task(current.model_copy(update={"title": "Changed at final CAS"}))
            return callback(tx)

        return await original_transaction(with_changed_source, write=write)

    monkeypatch.setattr(state, "transaction", mutate_origin_in_final_transaction)
    with pytest.raises(central_results.OriginTaskChangedConflict) as rejected:
        await central_results.commit_result(
            deps, runtime, consumer, result, token, final=True, origin_proofs=(proof,),
        )

    monkeypatch.setattr(state, "transaction", original_transaction)
    assert rejected.value.origin_task_id == ORIGIN
    assert rejected.value.diagnostic.evidence_ids == ["ev-cas-final"]
    assert content_hash(await state.get_task(CONSUMER)) == before_consumer
    # The competing update was staged in the same atomic callback and rolls back
    # with the rejected Task transition.
    assert content_hash(await state.get_task(ORIGIN)) == before_origin
    terminal_events = await original_transaction(lambda tx: tx.db.execute(
        "SELECT COUNT(*) FROM state_outbox WHERE run_id=? AND "
        "json_extract(payload, '$.event_type')='TaskCompleted'", (tx.run_id,),
    ).fetchone()[0], write=False)
    assert terminal_events == 0
