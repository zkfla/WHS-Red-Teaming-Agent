"""Evidence fences share the existing memory/SQLite coordination boundary."""

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from redteam.attack_graph.service import AttackGraphService
from redteam.attack_graph.stores.memory import InMemoryAttackGraphStore
from redteam.core.contracts.evidence import EvidenceRecord
from redteam.core.enums import EvidenceSourceType
from redteam.evidence.stores.filesystem import FilesystemEvidenceStore
from redteam.orchestration.coordination import StateConflict, coordinator_for
from redteam.orchestration.evidence_validity import refresh_validity
from redteam.orchestration.paths import SQLitePathStore
from redteam.orchestration.planning_admission import PlanningAdmission
from redteam.orchestration.planning_evidence import PlanningEvidenceStore
from redteam.orchestration.task_graph.sqlite import SQLiteTaskGraphStore
from tests.unit.test_orchestration_coordination import ENG, MISSION, RUN, world


def binding():
    return EvidenceRecord(
        evidence_id="binding", run_id=RUN, engagement_id=ENG,
        source_type=EvidenceSourceType.SYSTEM, source_id="orchestration.evidence_validity",
        content_type="application/json", created_at=datetime.now(timezone.utc), digest="bad",
        metadata={"record_kind": "evidence_validity_binding_v1", "claim_evidence_id": "original"},
    )


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
async def test_append_idempotency_conflict_and_late_plans(tmp_path, backend):
    w = await world(tmp_path, backend)
    admission = PlanningAdmission(w.coordinator)
    await admission.bind_inputs(RUN, {"scope": "synthetic"})
    stamp = await admission.capture(RUN, MISSION, reference_keys=["evidence"])
    store = PlanningEvidenceStore(FilesystemEvidenceStore(tmp_path / "evidence"), admission)
    record = binding()
    await store.append(record)
    versions = await admission.capture_references(RUN, ["evidence", "validity:original"])
    await store.append(record)
    assert await admission.capture_references(RUN, ["evidence", "validity:original"]) == versions
    assert len(await store.list_for_run(RUN)) == 1
    with pytest.raises(StateConflict, match="immutable_evidence_id_conflict"):
        await store.append(record.model_copy(update={"digest": "different"}))

    async def apply(tx):
        pytest.fail("the stale proposal must not write Tasks/outbox")

    with pytest.raises(StateConflict, match="stale_plan_reference_changed"):
        await admission.admit(stamp, proposal_id="late", proposal={}, apply=apply)


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
@pytest.mark.parametrize("failure", ["error", "cancel"])
async def test_unknown_append_retains_fence_across_reopen(tmp_path, backend, failure):
    w = await world(tmp_path, backend)
    base = FilesystemEvidenceStore(tmp_path / "evidence")

    class LostAcknowledgement:
        async def get(self, identity):
            return await base.get(identity)

        async def append(self, record):
            await base.append(record)
            if failure == "cancel":
                raise asyncio.CancelledError
            raise OSError("synthetic lost acknowledgement")

    store = PlanningEvidenceStore(LostAcknowledgement(), PlanningAdmission(w.coordinator))
    with pytest.raises(asyncio.CancelledError if failure == "cancel" else OSError):
        await store.append(binding())
    coordinator = (coordinator_for(SQLiteTaskGraphStore(w.db), SQLitePathStore(w.db))
                   if backend == "sqlite" else w.coordinator)
    with pytest.raises(StateConflict, match="planning_reference_update_pending"):
        await PlanningAdmission(coordinator).capture_references(RUN, ["validity:original"])
    assert (await FilesystemEvidenceStore(tmp_path / "evidence").get("binding")) is not None


async def test_reopened_store_and_coordinator_deduplicate_validity_outbox(tmp_path):
    w = await world(tmp_path, "sqlite")
    base = FilesystemEvidenceStore(tmp_path / "evidence")
    store = PlanningEvidenceStore(base, PlanningAdmission(w.coordinator))
    await store.append(binding())
    deps = SimpleNamespace(coordinator=w.coordinator, task_graph_store=w.tasks, path_store=w.paths,
                           evidence_store=store, graph_store=InMemoryAttackGraphStore(),
                           event_bus=w.bus)
    runtime = SimpleNamespace(run_id=RUN, mission_id=MISSION, engagement_id=ENG)
    current = await refresh_validity(deps, runtime)
    assert current[0].status == "invalid"
    assert len(w.bus.history()) == 1
    deps.task_graph_store, deps.path_store = SQLiteTaskGraphStore(w.db), SQLitePathStore(w.db)
    deps.coordinator = coordinator_for(deps.task_graph_store, deps.path_store)
    deps.evidence_store = PlanningEvidenceStore(
        FilesystemEvidenceStore(tmp_path / "evidence"), PlanningAdmission(deps.coordinator),
    )
    assert await refresh_validity(deps, runtime) == current
    assert len(w.bus.history()) == 1
    assert await AttackGraphService(deps.graph_store).list_findings() == []


async def test_plan_with_elapsed_observed_validity_does_not_admit(tmp_path):
    w = await world(tmp_path, "memory")
    admission = PlanningAdmission(w.coordinator)
    await admission.bind_inputs(RUN, {"scope": "synthetic"})
    stamp = await admission.capture(RUN, MISSION)
    stamp = replace(stamp, valid_until=datetime.now(timezone.utc) - timedelta(seconds=1))

    async def apply(tx):
        pytest.fail("no writes from an expired valid-evidence snapshot")

    with pytest.raises(StateConflict, match="stale_plan_evidence_expired"):
        await admission.admit(stamp, proposal_id="expired", proposal={}, apply=apply)
