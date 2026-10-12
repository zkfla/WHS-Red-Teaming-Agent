"""A later worker's retraction and observations cannot use an earlier local clock."""

from redteam.attack_graph.predicates import attrs_of
from redteam.core.enums import GraphNodeType
from tests.fakes.graph_records import records_for, seed_records
from tests.unit.test_plan4f_graph_lineage import local_service, measured


async def test_later_lower_step_retraction_blocks_original_redelivery():
    service = local_service()
    node = await service.upsert_node(
        GraphNodeType.HOST, key="a.local", attrs={"reachable": True},
        provenance=measured(5, "ev-a"), step=5,
    )
    await service.retract(node.id, "reachable", step=9, reason="first withdrawal")
    await service.upsert_node(
        GraphNodeType.HOST, key="a.local", attrs={"reachable": True},
        provenance=measured(1, "ev-b"), step=1,
    )
    await service.retract(node.id, "reachable", step=2, reason="later worker withdrawal")
    await service.upsert_node(
        GraphNodeType.HOST, key="a.local", attrs={"reachable": True},
        provenance=measured(3, "ev-b"), step=3,
    )
    assert "reachable" not in attrs_of((await service.get_node(node.id)).properties)


async def test_retraction_snapshot_does_not_remove_later_lower_step_observation():
    service = local_service()
    node = await service.upsert_node(
        GraphNodeType.HOST, key="a.local", attrs={"reachable": True},
        provenance=measured(5, "ev-a"), step=5,
    )
    await service.retract(node.id, "reachable", step=9, reason="first withdrawal")
    stale_withdrawal = await records_for(service)
    await service.upsert_node(
        GraphNodeType.HOST, key="a.local", attrs={"reachable": True},
        provenance=measured(1, "ev-b"), step=1,
    )
    await seed_records(service, stale_withdrawal, merge=True)
    assert attrs_of((await service.get_node(node.id)).properties).get("reachable") is True
