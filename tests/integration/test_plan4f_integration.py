"""Plan 4f final integration (F20 slice): workers sharing central SQLite.

Writer to shared-graph query path at the central level: a later worker
re-observation restores through the same store another worker retracted
through, the observe() projection seen by planning agrees, a stale snapshot
import stays withdrawn, and a reopened run keeps the lineage.
"""

from __future__ import annotations

from redteam.app.bootstrap import build_central_graph
from redteam.attack_graph.predicates import attrs_of
from redteam.attack_graph.service import AttackGraphService
from redteam.core.enums import GraphNodeType
from redteam.orchestration.central_state import CentralStateStore
from tests.fakes.graph_records import records_for, seed_records
from tests.unit.test_central_state import ENG, RUN, world
from tests.unit.test_plan4f_graph_lineage import measured


def central_service(state):
    return AttackGraphService(build_central_graph(state))


async def test_two_workers_share_lineage_through_central_store(tmp_path):
    state = await world(tmp_path)
    worker_a = central_service(state)
    worker_b = central_service(state)
    node = await worker_a.upsert_node(GraphNodeType.HOST, key="a.local",
                                      attrs={"reachable": True},
                                      provenance=measured(5, "ev-a"), step=5)
    await worker_a.retract(node.id, "reachable", step=9, reason="first withdrawal")
    await worker_b.upsert_node(GraphNodeType.HOST, key="a.local",
                               attrs={"reachable": True},
                               provenance=measured(1, "ev-b"), step=1)
    seen = await worker_a.get_node(node.id)
    assert seen is not None
    assert attrs_of(seen.properties)["reachable"] is True
    found = await worker_a.query(node_type=GraphNodeType.HOST, attrs={"reachable": True})
    assert [item.id for item in found] == [node.id]
    projection = await worker_a.observe(step=10)
    assert any("+reachable" in line
               for lines in projection.nodes_by_type.values() for line in lines)


async def test_stale_snapshot_then_restore_through_central_store(tmp_path):
    state = await world(tmp_path)
    svc = central_service(state)
    node = await svc.upsert_node(GraphNodeType.HOST, key="a.local",
                                 attrs={"reachable": True},
                                 provenance=measured(5, "ev-a"), step=5)
    stale = await records_for(svc)
    await svc.retract(node.id, "reachable", step=9, reason="first withdrawal")
    await seed_records(svc, stale, merge=True)
    assert "reachable" not in attrs_of((await svc.get_node(node.id)).properties)
    await svc.upsert_node(GraphNodeType.HOST, key="a.local",
                          attrs={"reachable": True},
                          provenance=measured(1, "ev-b"), step=1)
    assert attrs_of((await svc.get_node(node.id)).properties)["reachable"] is True


async def test_reopened_run_keeps_lineage(tmp_path):
    state = await world(tmp_path)
    svc = central_service(state)
    node = await svc.upsert_node(GraphNodeType.HOST, key="a.local",
                                 attrs={"reachable": True},
                                 provenance=measured(5, "ev-a"), step=5)
    await svc.retract(node.id, "reachable", step=9, reason="first withdrawal")
    reopened = CentralStateStore(tmp_path / "state.db", run_id=RUN, engagement_id=ENG)
    await reopened.setup()
    later = central_service(reopened)
    kept = await later.get_node(node.id)
    assert kept is not None
    assert "reachable" not in attrs_of(kept.properties)
    (entry,) = kept.properties["retractions"]
    assert entry["supersedes_version"] == 2
    assert {
        (item["run_id"], item["evidence_id"]) for item in entry["withdrawn"]
    } == {("run-1", "ev-a")}
    await later.upsert_node(GraphNodeType.HOST, key="a.local",
                            attrs={"reachable": True},
                            provenance=measured(1, "ev-b"), step=1)
    assert attrs_of((await later.get_node(node.id)).properties)["reachable"] is True
