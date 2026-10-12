"""Plan 4f (F20): observation lineage across workers sharing one graph.

Worker-local step counters share one numeric axis, so after worker A retracts
a predicate at its step 9, worker B genuine step-1 re-observation used to be
dropped as older. Retraction entries now record the withdrawn observation
lineage: a genuinely new observation restores the predicate while a stale
pre-retraction snapshot replay stays withdrawn.
"""

from __future__ import annotations

import asyncio

from redteam.app.bootstrap import build_central_graph
from redteam.attack_graph.predicates import (
    Confidence,
    Provenance,
    apply_retraction,
    attrs_of,
    build_properties,
    claim_provenance_of,
    merge_properties,
)
from redteam.attack_graph.service import AttackGraphService
from redteam.attack_graph.stores.memory import InMemoryAttackGraphStore
from redteam.attack_graph.stores.namespaced import EngagementScopedAttackGraphStore
from redteam.core.enums import GraphNodeType
from redteam.core.ids import EngagementId
from tests.fakes.graph_records import records_for, seed_records
from tests.unit.test_central_state import world


def measured(step, evidence, run="run-1"):
    return Provenance(step=step, source="local:graph.record", evidence_id=evidence,
                      run_id=run, confidence=Confidence.MEASURED)


def local_service(store=None):
    return AttackGraphService(store or InMemoryAttackGraphStore())


async def test_late_worker_reobservation_restores_despite_smaller_step():
    svc = local_service()
    node = await svc.upsert_node(GraphNodeType.HOST, key="a.local",
                                 attrs={"reachable": True},
                                 provenance=measured(5, "ev-a-1"), step=5)
    await svc.retract(node.id, "reachable", step=9, reason="stopped answering")
    assert "reachable" not in attrs_of((await svc.get_node(node.id)).properties)
    await svc.upsert_node(GraphNodeType.HOST, key="a.local",
                          attrs={"reachable": True},
                          provenance=measured(1, "ev-b-1"), step=1)
    refreshed = await svc.get_node(node.id)
    assert refreshed is not None
    assert attrs_of(refreshed.properties)["reachable"] is True


async def test_pre_retraction_snapshot_import_stays_withdrawn():
    svc = local_service()
    node = await svc.upsert_node(GraphNodeType.HOST, key="a.local",
                                 attrs={"reachable": True},
                                 provenance=measured(5, "ev-a-1"), step=5)
    stale = await records_for(svc)
    await svc.retract(node.id, "reachable", step=9, reason="stopped answering")
    await seed_records(svc, stale, merge=True)
    refreshed = await svc.get_node(node.id)
    assert refreshed is not None
    assert "reachable" not in attrs_of(refreshed.properties)


async def test_same_evidence_redelivery_stays_withdrawn():
    svc = local_service()
    node = await svc.upsert_node(GraphNodeType.HOST, key="a.local",
                                 attrs={"reachable": True},
                                 provenance=measured(5, "ev-a-1"), step=5)
    await svc.retract(node.id, "reachable", step=9, reason="stopped answering")
    await svc.upsert_node(GraphNodeType.HOST, key="a.local",
                          attrs={"reachable": True},
                          provenance=measured(12, "ev-a-1"), step=12)
    refreshed = await svc.get_node(node.id)
    assert refreshed is not None
    assert "reachable" not in attrs_of(refreshed.properties)


async def test_unsourced_write_keeps_numeric_order():
    svc = local_service()
    node = await svc.upsert_node(GraphNodeType.HOST, key="a.local",
                                 attrs={"reachable": True},
                                 provenance=measured(5, "ev-a-1"), step=5)
    await svc.retract(node.id, "reachable", step=9, reason="stopped answering")
    await svc.upsert_node(GraphNodeType.HOST, key="a.local",
                          attrs={"reachable": True}, step=3)
    assert "reachable" not in attrs_of((await svc.get_node(node.id)).properties)
    await svc.upsert_node(GraphNodeType.HOST, key="a.local",
                          attrs={"reachable": True}, step=12)
    assert attrs_of((await svc.get_node(node.id)).properties)["reachable"] is True


async def test_legacy_retraction_entry_stays_conservative():
    existing = build_properties(attrs={"reachable": True}, step=5)
    incoming, _ = apply_retraction(dict(existing), "reachable", step=5, reason="x")
    incoming["retractions"] = [{"step": 9, "key": "reachable", "reason": "x"}]
    assert "withdrawn" not in incoming["retractions"][0]
    late = build_properties(attrs={"reachable": True},
                            provenance=measured(1, "ev-b-1"), step=1)
    assert "reachable" not in merge_properties(incoming, late)["attrs"]
    newer = build_properties(attrs={"reachable": True},
                             provenance=measured(12, "ev-b-1"), step=12)
    assert merge_properties(incoming, newer)["attrs"]["reachable"] is True


async def test_parallel_row_updates_merge_without_loss():
    svc = local_service()
    node = await svc.upsert_node(GraphNodeType.HOST, key="a.local",
                                 attrs={"reachable": True},
                                 provenance=measured(1, "ev-a-1"), step=1)
    first = measured(2, "ev-a-2")
    second = measured(2, "ev-a-3")
    await asyncio.gather(
        svc.upsert_node(GraphNodeType.HOST, key="a.local", attrs={"probed": True},
                        provenance=first, step=2),
        svc.upsert_node(GraphNodeType.HOST, key="a.local", attrs={"tls": True},
                        provenance=second, step=2),
    )
    refreshed = await svc.get_node(node.id)
    assert refreshed is not None
    assert attrs_of(refreshed.properties) == {"reachable": True, "probed": True, "tls": True}


async def test_retraction_records_withdrawn_lineage_and_retractor():
    svc = local_service()
    node = await svc.upsert_node(GraphNodeType.HOST, key="a.local",
                                 attrs={"reachable": True},
                                 provenance=measured(5, "ev-a-1"), step=5)
    by = Provenance(step=9, source="op:review", evidence_id="ev-r-1",
                    run_id="run-1", confidence=Confidence.INFERRED)
    assert await svc.retract(node.id, "reachable", step=9,
                             reason="stopped answering", provenance=by) is True
    refreshed = await svc.get_node(node.id)
    assert refreshed is not None
    (entry,) = refreshed.properties["retractions"]
    assert entry["step"] == 9 and entry["key"] == "reachable"
    assert entry["lineage_version"] == 1
    assert entry["supersedes_version"] == 2
    assert {(item["run_id"], item["evidence_id"], item["source"])
            for item in entry["withdrawn"]} == {("run-1", "ev-a-1", "local:graph.record")}
    assert entry["retracted_by"]["source"] == "op:review"


async def test_restore_points_claim_at_new_lineage():
    svc = local_service()
    node = await svc.upsert_node(GraphNodeType.HOST, key="a.local",
                                 attrs={"reachable": True},
                                 provenance=measured(5, "ev-a-1"), step=5)
    await svc.retract(node.id, "reachable", step=9, reason="stopped answering")
    await svc.upsert_node(GraphNodeType.HOST, key="a.local",
                          attrs={"reachable": True},
                          provenance=measured(1, "ev-b-1"), step=1)
    refreshed = await svc.get_node(node.id)
    assert refreshed is not None
    claim = claim_provenance_of(refreshed.properties)["attrs:reachable"]
    assert claim["evidence_id"] == "ev-b-1" and claim["step"] == 1


async def test_resume_round_trip_then_new_observation_restores():
    svc = local_service()
    node = await svc.upsert_node(GraphNodeType.HOST, key="a.local",
                                 attrs={"reachable": True},
                                 provenance=measured(5, "ev-a-1"), step=5)
    await svc.retract(node.id, "reachable", step=9, reason="stopped answering")
    records = await records_for(svc)
    resumed = local_service()
    await seed_records(resumed, records)
    assert "reachable" not in attrs_of((await resumed.get_node(node.id)).properties)
    await resumed.upsert_node(GraphNodeType.HOST, key="a.local",
                              attrs={"reachable": True},
                              provenance=measured(1, "ev-b-1"), step=1)
    assert attrs_of((await resumed.get_node(node.id)).properties)["reachable"] is True


async def test_two_services_over_shared_store_restore_on_query_path():
    shared = InMemoryAttackGraphStore()
    engagement = EngagementId("eng-f20")
    svc_a = AttackGraphService(EngagementScopedAttackGraphStore(shared, engagement_id=engagement))
    svc_b = AttackGraphService(EngagementScopedAttackGraphStore(shared, engagement_id=engagement))
    node = await svc_a.upsert_node(GraphNodeType.HOST, key="a.local",
                                   attrs={"reachable": True},
                                   provenance=measured(5, "ev-a-1"), step=5)
    await svc_a.retract(node.id, "reachable", step=9, reason="stopped answering")
    await svc_b.upsert_node(GraphNodeType.HOST, key="a.local",
                            attrs={"reachable": True},
                            provenance=measured(1, "ev-b-1"), step=1)
    assert attrs_of((await svc_a.get_node(node.id)).properties)["reachable"] is True
    found = await svc_a.query(node_type=GraphNodeType.HOST, attrs={"reachable": True})
    assert [item.id for item in found] == [node.id]


async def test_central_store_lineage_restore_and_stale_block(tmp_path):
    state = await world(tmp_path)
    svc = AttackGraphService(build_central_graph(state))
    node = await svc.upsert_node(GraphNodeType.HOST, key="a.local",
                                 attrs={"reachable": True},
                                 provenance=measured(5, "ev-a-1"), step=5)
    stale = await records_for(svc)
    await svc.retract(node.id, "reachable", step=9, reason="stopped answering")
    await seed_records(svc, stale, merge=True)
    assert "reachable" not in attrs_of((await svc.get_node(node.id)).properties)
    await svc.upsert_node(GraphNodeType.HOST, key="a.local",
                          attrs={"reachable": True},
                          provenance=measured(1, "ev-b-1"), step=1)
    assert attrs_of((await svc.get_node(node.id)).properties)["reachable"] is True


async def test_unseen_fork_snapshot_import_stays_withdrawn():
    main = local_service()
    node = await main.upsert_node(GraphNodeType.HOST, key="a.local",
                                  attrs={"reachable": True},
                                  provenance=measured(5, "ev-x"), step=5)
    fork = local_service()
    await seed_records(fork, await records_for(main))
    await fork.upsert_node(GraphNodeType.HOST, key="a.local",
                           attrs={"reachable": True},
                           provenance=measured(6, "ev-y"), step=6)
    await main.retract(node.id, "reachable", step=9, reason="stopped answering")
    await seed_records(main, await records_for(fork), merge=True)
    refreshed = await main.get_node(node.id)
    assert refreshed is not None
    assert "reachable" not in attrs_of(refreshed.properties)


async def test_fork_branched_after_retraction_restores():
    main = local_service()
    node = await main.upsert_node(GraphNodeType.HOST, key="a.local",
                                  attrs={"reachable": True},
                                  provenance=measured(5, "ev-x"), step=5)
    await main.retract(node.id, "reachable", step=9, reason="stopped answering")
    fork = local_service()
    await seed_records(fork, await records_for(main))
    await fork.upsert_node(GraphNodeType.HOST, key="a.local",
                           attrs={"reachable": True},
                           provenance=measured(1, "ev-z"), step=1)
    await seed_records(main, await records_for(fork), merge=True)
    refreshed = await main.get_node(node.id)
    assert refreshed is not None
    assert attrs_of(refreshed.properties)["reachable"] is True


async def test_identical_redelivery_does_not_advance_version():
    svc = local_service()
    node = await svc.upsert_node(GraphNodeType.HOST, key="a.local",
                                 attrs={"reachable": True},
                                 provenance=measured(5, "ev-a-1"), step=5)
    first = (await svc.get_node(node.id)).properties["observation_version"]
    await svc.upsert_node(GraphNodeType.HOST, key="a.local",
                          attrs={"reachable": True},
                          provenance=measured(5, "ev-a-1"), step=5)
    again = await svc.get_node(node.id)
    assert again is not None
    assert again.properties["observation_version"] == first
    assert attrs_of(again.properties) == {"reachable": True}
