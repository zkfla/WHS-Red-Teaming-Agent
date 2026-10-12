"""The read-only boundary, integrity checks, and snapshot failure modes."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from redteam.attack_graph.integrity import check_integrity
from redteam.attack_graph.predicates import Confidence, Provenance, build_properties
from redteam.attack_graph.service import AttackGraphService
from redteam.attack_graph.snapshot import SnapshotError, from_records, to_records
from redteam.attack_graph.stores.base import StoreCapabilityError, require_listable
from redteam.attack_graph.stores.memory import InMemoryAttackGraphStore
from redteam.attack_graph.view import AttackGraphView
from redteam.core.contracts.graph import Finding, GraphEdge, GraphNode, Hypothesis
from redteam.core.enums import GraphEdgeType, GraphNodeType
from redteam.core.ids import GraphEdgeId, GraphNodeId
from tests.fakes.graph_records import records_for

NOW = datetime.now(timezone.utc)


class NodeOnlyStore:
    """Satisfies the frozen core port but cannot enumerate."""

    def __init__(self) -> None:
        self._nodes: dict[GraphNodeId, GraphNode] = {}

    async def add_node(self, node: GraphNode) -> None:
        self._nodes[node.id] = node

    async def add_edge(self, edge: GraphEdge) -> None: ...

    async def get_node(self, node_id: GraphNodeId) -> GraphNode | None:
        return self._nodes.get(node_id)

    async def get_edge(self, edge_id: GraphEdgeId) -> GraphEdge | None:
        return None

    async def neighbors(self, node_id: GraphNodeId) -> list[GraphNode]:
        return []

    async def query(self, **filters: object) -> list[GraphNode]:
        return list(self._nodes.values())


def graph_node(node_id: str, node_type: GraphNodeType, properties: dict | None = None) -> GraphNode:
    return GraphNode(
        id=GraphNodeId(node_id),
        type=node_type,
        properties=properties if properties is not None else build_properties(),
        created_at=NOW,
        updated_at=NOW,
    )


# -- read-only boundary ---------------------------------------------------


async def test_the_view_exposes_reads_and_nothing_else() -> None:
    store = InMemoryAttackGraphStore()
    service = AttackGraphService(store)
    await service.upsert_node(GraphNodeType.HOST, key="a.local")
    view = service.view()

    assert isinstance(view, AttackGraphView)
    assert len(await view.list_nodes()) == 1
    assert await view.counts() == {"nodes": 1, "edges": 0}

    for write_method in ("upsert_node", "add_node", "record_finding", "link", "retract"):
        with pytest.raises(AttributeError, match="read-only"):
            getattr(view, write_method)


async def test_view_has_edge_answers_relation_questions() -> None:
    service = AttackGraphService(InMemoryAttackGraphStore())
    host = await service.upsert_node(GraphNodeType.HOST, key="a.local")
    svc = await service.upsert_node(GraphNodeType.SERVICE, key="a.local/ssh")
    await service.link(GraphEdgeType.HOSTS, host.id, svc.id)

    view = service.view()
    assert await view.has_edge(host.id, svc.id, GraphEdgeType.HOSTS)
    assert not await view.has_edge(svc.id, host.id, GraphEdgeType.HOSTS)


async def test_a_store_that_cannot_enumerate_says_so_clearly() -> None:
    # The frozen core port has no list_nodes/list_edges; a store without
    # them gets an explicit error rather than a silently empty answer.
    service = AttackGraphService(NodeOnlyStore())
    await service.upsert_node(GraphNodeType.HOST, key="a.local")

    assert await service.get_node(GraphNodeId("host:a.local")) is not None
    with pytest.raises(StoreCapabilityError, match="ListableAttackGraphStore"):
        await service.observe()
    with pytest.raises(StoreCapabilityError):
        await records_for(service)


def test_require_listable_accepts_the_in_memory_store() -> None:
    assert require_listable(InMemoryAttackGraphStore(), "list_nodes") is not None


async def test_unknown_query_filters_raise_rather_than_matching_everything() -> None:
    with pytest.raises(TypeError, match="unsupported query filters"):
        await InMemoryAttackGraphStore().query(colour="red")


async def test_query_filters_by_label_and_attrs() -> None:
    service = AttackGraphService(InMemoryAttackGraphStore())
    await service.upsert_node(
        GraphNodeType.HOST, key="a.local", labels=["scope"], attrs={"in_scope": True}
    )
    await service.upsert_node(GraphNodeType.HOST, key="b.local")

    assert len(await service.query(label="scope")) == 1
    assert len(await service.query(attrs={"in_scope": True})) == 1
    assert len(await service.query(node_type=GraphNodeType.HOST)) == 2


# -- integrity ------------------------------------------------------------


def test_a_clean_graph_reports_clean() -> None:
    props = build_properties(
        provenance=Provenance(step=1, source="tool:x", confidence=Confidence.MEASURED)
    )
    report = check_integrity([graph_node("host:a", GraphNodeType.HOST, props)], [])
    assert report.is_clean
    assert report.describe() == "clean"


def test_a_verified_finding_without_evidence_is_flagged() -> None:
    finding = Finding(finding_id="f1", title="t", verified=True, evidence_ids=[])
    node = graph_node(
        "finding:f1",
        GraphNodeType.FINDING,
        build_properties(
            facts=finding.model_dump(mode="json"),
            provenance=Provenance(step=1, source="x", confidence=Confidence.MEASURED),
        ),
    )
    report = check_integrity([node], [])
    assert report.ungrounded_findings
    assert any("ungrounded finding" in item for item in report.blocking)


def test_unsourced_and_weakly_grounded_nodes_are_separated() -> None:
    unsourced = graph_node("host:a", GraphNodeType.HOST)
    weak = graph_node(
        "host:b",
        GraphNodeType.HOST,
        build_properties(provenance=Provenance(step=1, source="guess")),
    )
    report = check_integrity([unsourced, weak], [])

    assert report.unsourced_nodes == ["host:a"]
    assert report.weakly_grounded_nodes == ["host:b"]
    # Neither blocks a report on its own — they are informational.
    assert report.blocking == []


def test_dangling_edges_and_malformed_ids_block() -> None:
    edge = GraphEdge(
        id=GraphEdgeId("e1"),
        type=GraphEdgeType.HOSTS,
        source_id=GraphNodeId("host:a"),
        target_id=GraphNodeId("service:missing"),
        created_at=NOW,
    )
    report = check_integrity([graph_node("service:a", GraphNodeType.HOST)], [edge])

    assert report.dangling_edges
    # id says "service", the node says HOST — this would never dedup.
    assert report.malformed_node_ids
    assert len(report.blocking) >= 2


def test_open_hypotheses_are_reported_but_do_not_block() -> None:
    hypothesis = Hypothesis(hypothesis_id="h1", description="d", status="open")
    node = graph_node(
        "hypothesis:h1",
        GraphNodeType.HYPOTHESIS,
        build_properties(
            facts=hypothesis.model_dump(mode="json"),
            provenance=Provenance(step=1, source="agent", confidence=Confidence.ASSERTED),
        ),
    )
    report = check_integrity([node], [])
    assert report.unresolved_hypotheses == ["h1"]
    assert report.blocking == []


# -- snapshots ------------------------------------------------------------


def test_snapshot_export_order_is_deterministic() -> None:
    nodes = [graph_node("host:b", GraphNodeType.HOST), graph_node("host:a", GraphNodeType.HOST)]
    serialized = to_records(nodes, [])
    restored_nodes, restored_edges, _ = from_records(serialized)
    assert to_records(restored_nodes, restored_edges) == serialized
    ids = [record["id"] for record in to_records(nodes, []) if record["_collection"] == "nodes"]
    assert ids == ["host:a", "host:b"]


def test_an_unknown_collection_raises_instead_of_being_dropped() -> None:
    # Silently dropping records means a report can go out missing
    # evidence with nobody aware of it.
    with pytest.raises(SnapshotError, match="unknown collection"):
        from_records([{"_collection": "mystery", "x": 1}])


def test_an_unsupported_schema_version_raises() -> None:
    with pytest.raises(SnapshotError, match="unsupported snapshot schema version"):
        from_records([{"_collection": "graph_meta", "schema_version": 99}])
