"""Durable SQLite Attack Graph store: restart recovery, tenancy, fail-closed schema.

Targeted tests for ``redteam.attack_graph.stores.sqlite.SQLiteAttackGraphStore``
only — the in-memory/store contract behavior is already covered by
``tests/unit/test_attack_graph.py``.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

import pytest

from redteam.attack_graph.snapshot import (
    COLLECTION_KEY,
    META_COLLECTION,
    SNAPSHOT_SCHEMA_VERSION,
    SnapshotError,
)
from redteam.attack_graph.stores.base import (
    ListableAttackGraphStore,
    MutableAttackGraphStore,
    require_listable,
    require_mutable,
)
from redteam.attack_graph.stores.namespaced import EngagementScopedAttackGraphStore
from redteam.attack_graph.stores.sqlite import (
    AttackGraphSchemaError,
    SQLiteAttackGraphStore,
)
from redteam.core.contracts.graph import GraphEdge, GraphNode
from redteam.core.enums import GraphEdgeType, GraphNodeType
from redteam.core.ids import EngagementId, GraphEdgeId, GraphNodeId

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def node(
    node_id: str,
    node_type: GraphNodeType = GraphNodeType.HOST,
    *,
    labels: list[str] | None = None,
    attrs: dict | None = None,
) -> GraphNode:
    properties = {"attrs": attrs} if attrs is not None else {}
    return GraphNode(
        id=GraphNodeId(node_id),
        type=node_type,
        labels=labels or [],
        properties=properties,
        created_at=NOW,
        updated_at=NOW,
    )


def edge(
    edge_id: str,
    source: str,
    target: str,
    *,
    edge_type: GraphEdgeType = GraphEdgeType.HOSTS,
) -> GraphEdge:
    return GraphEdge(
        id=GraphEdgeId(edge_id),
        type=edge_type,
        source_id=GraphNodeId(source),
        target_id=GraphNodeId(target),
        created_at=NOW,
    )


def _records(path) -> list[dict]:
    with sqlite3.connect(path) as db:
        rows = db.execute("SELECT record FROM graph_records").fetchall()
    return [json.loads(row[0]) for row in rows]


def _set_meta_version(path, version) -> None:
    with sqlite3.connect(path) as db:
        rows = db.execute(
            "SELECT record FROM graph_records WHERE collection=?", (META_COLLECTION,)
        ).fetchall()
        assert len(rows) == 1
        body = json.loads(rows[0][0])
        body["schema_version"] = version
        db.execute(
            "UPDATE graph_records SET record=? WHERE collection=?",
            (json.dumps(body), META_COLLECTION),
        )
        db.commit()


# --- restart recovery --------------------------------------------------


async def test_graph_survives_process_restart(tmp_path) -> None:
    path = tmp_path / "attack-graph.sqlite3"
    first = SQLiteAttackGraphStore(path)
    await first.add_node(node("host:web.local"))
    await first.add_node(node("svc:web.local:443", GraphNodeType.SERVICE))
    await first.add_edge(edge("edge:1", "host:web.local", "svc:web.local:443"))

    # A brand-new instance stands in for a new process reading the file.
    second = SQLiteAttackGraphStore(path)
    assert await second.get_node(GraphNodeId("host:web.local")) == node("host:web.local")
    assert await second.get_edge(GraphEdgeId("edge:1")) == edge(
        "edge:1", "host:web.local", "svc:web.local:443"
    )
    assert [n.id for n in await second.neighbors(GraphNodeId("host:web.local"))] == [
        GraphNodeId("svc:web.local:443")
    ]
    assert {str(n.id) for n in await second.list_nodes()} == {
        "host:web.local",
        "svc:web.local:443",
    }
    assert {str(e.id) for e in await second.list_edges()} == {"edge:1"}


async def test_queries_and_labels_survive_restart(tmp_path) -> None:
    path = tmp_path / "query.sqlite3"
    first = SQLiteAttackGraphStore(path)
    await first.add_node(node("host:a", labels=["scoped"], attrs={"reachable": True}))
    await first.add_node(node("svc:a:80", GraphNodeType.SERVICE))

    second = SQLiteAttackGraphStore(path)
    assert [str(n.id) for n in await second.query(node_type=GraphNodeType.HOST)] == ["host:a"]
    assert [str(n.id) for n in await second.query(label="scoped")] == ["host:a"]
    assert [str(n.id) for n in await second.query(attrs={"reachable": True})] == ["host:a"]
    with pytest.raises(TypeError):
        await second.query(unsupported=True)


async def test_edge_upsert_replaces_endpoints(tmp_path) -> None:
    path = tmp_path / "upsert.sqlite3"
    store = SQLiteAttackGraphStore(path)
    await store.add_node(node("host:a"))
    await store.add_node(node("svc:a:80", GraphNodeType.SERVICE))
    await store.add_node(node("svc:a:443", GraphNodeType.SERVICE))
    await store.add_edge(edge("edge:1", "host:a", "svc:a:80"))
    await store.add_edge(edge("edge:1", "host:a", "svc:a:443"))

    reloaded = SQLiteAttackGraphStore(path)
    fetched = await reloaded.get_edge(GraphEdgeId("edge:1"))
    assert fetched is not None and fetched.target_id == GraphNodeId("svc:a:443")
    assert [str(n.id) for n in await reloaded.neighbors(GraphNodeId("host:a"))] == ["svc:a:443"]


async def test_node_upsert_replaces_payload(tmp_path) -> None:
    path = tmp_path / "node-upsert.sqlite3"
    store = SQLiteAttackGraphStore(path)
    await store.add_node(node("host:a", labels=["old"]))
    await store.add_node(node("host:a", labels=["new"]))

    reloaded = SQLiteAttackGraphStore(path)
    fetched = await reloaded.get_node(GraphNodeId("host:a"))
    assert fetched is not None and fetched.labels == ["new"]


# --- mutability --------------------------------------------------------


async def test_remove_node_drops_incident_edges_and_persists(tmp_path) -> None:
    path = tmp_path / "remove.sqlite3"
    store = SQLiteAttackGraphStore(path)
    await store.add_node(node("host:a"))
    await store.add_node(node("svc:a:80", GraphNodeType.SERVICE))
    await store.add_edge(edge("edge:1", "host:a", "svc:a:80"))

    assert await store.remove_node(GraphNodeId("host:a")) is True
    assert await store.remove_node(GraphNodeId("host:a")) is False

    reloaded = SQLiteAttackGraphStore(path)
    assert await reloaded.get_node(GraphNodeId("host:a")) is None
    assert await reloaded.get_edge(GraphEdgeId("edge:1")) is None
    # The remote endpoint survives on its own.
    assert await reloaded.get_node(GraphNodeId("svc:a:80")) is not None


async def test_remove_edge_persists(tmp_path) -> None:
    path = tmp_path / "remove-edge.sqlite3"
    store = SQLiteAttackGraphStore(path)
    await store.add_node(node("host:a"))
    await store.add_node(node("svc:a:80", GraphNodeType.SERVICE))
    await store.add_edge(edge("edge:1", "host:a", "svc:a:80"))

    assert await store.remove_edge(GraphEdgeId("edge:1")) is True
    assert await store.remove_edge(GraphEdgeId("edge:1")) is False

    reloaded = SQLiteAttackGraphStore(path)
    assert await reloaded.get_edge(GraphEdgeId("edge:1")) is None
    assert await reloaded.list_edges() == []


# --- capability protocols ---------------------------------------------


def test_store_declares_listable_and_mutable(tmp_path) -> None:
    store = SQLiteAttackGraphStore(tmp_path / "caps.sqlite3")
    assert isinstance(store, ListableAttackGraphStore)
    assert isinstance(store, MutableAttackGraphStore)
    assert require_listable(store, "list_nodes") is store
    assert require_mutable(store, "remove_node") is store


# --- tenancy -----------------------------------------------------------


async def test_engagement_tenancy_is_preserved_across_restart(tmp_path) -> None:
    path = tmp_path / "shared.sqlite3"
    first = SQLiteAttackGraphStore(path)
    store_a = EngagementScopedAttackGraphStore(first, engagement_id=EngagementId("eng-a"))
    store_b = EngagementScopedAttackGraphStore(first, engagement_id=EngagementId("eng-b"))

    await store_a.add_node(node("host:example.local"))
    await store_b.add_node(node("host:example.local"))

    # Same un-namespaced id in two engagements must not collide on disk.
    reloaded = SQLiteAttackGraphStore(path)
    restarted_a = EngagementScopedAttackGraphStore(
        reloaded, engagement_id=EngagementId("eng-a")
    )
    restarted_b = EngagementScopedAttackGraphStore(
        reloaded, engagement_id=EngagementId("eng-b")
    )
    assert [str(n.id) for n in await restarted_a.list_nodes()] == ["host:example.local"]
    assert [str(n.id) for n in await restarted_b.list_nodes()] == ["host:example.local"]

    # The underlying store holds two distinct, fully-qualified rows.
    assert len(await reloaded.list_nodes()) == 2
    ids = {str(n.id) for n in await reloaded.list_nodes()}
    assert ids == {"eng-a\x1fhost:example.local", "eng-b\x1fhost:example.local"}

    # A removal in one engagement leaves the other untouched.
    assert await restarted_a.remove_node(GraphNodeId("host:example.local")) is True
    assert len(await restarted_b.list_nodes()) == 1


# --- schema version fail-closed ---------------------------------------


async def test_fresh_database_is_stamped_with_supported_version(tmp_path) -> None:
    path = tmp_path / "stamped.sqlite3"
    store = SQLiteAttackGraphStore(path)
    await store.add_node(node("host:a"))

    meta = [r for r in _records(path) if r.get(COLLECTION_KEY) == META_COLLECTION]
    assert meta == [{COLLECTION_KEY: META_COLLECTION, "schema_version": SNAPSHOT_SCHEMA_VERSION}]


@pytest.mark.parametrize("bad_version", [999, "future", None])
async def test_unsupported_schema_version_fails_closed(tmp_path, bad_version) -> None:
    path = tmp_path / f"bad-{str(bad_version)}.sqlite3"
    store = SQLiteAttackGraphStore(path)
    await store.add_node(node("host:a"))
    _set_meta_version(path, bad_version)

    with pytest.raises(AttackGraphSchemaError) as excinfo:
        await SQLiteAttackGraphStore(path).setup()
    assert isinstance(excinfo.value, SnapshotError)


async def test_data_without_schema_header_fails_closed(tmp_path) -> None:
    path = tmp_path / "no-header.sqlite3"
    store = SQLiteAttackGraphStore(path)
    await store.add_node(node("host:a"))

    with sqlite3.connect(path) as db:
        db.execute("DELETE FROM graph_records WHERE collection=?", (META_COLLECTION,))
        db.commit()

    with pytest.raises(AttackGraphSchemaError):
        await SQLiteAttackGraphStore(path).setup()


async def test_empty_database_without_header_is_initialized(tmp_path) -> None:
    # No data and no header is a brand-new store, not a fail-closed case.
    path = tmp_path / "empty.sqlite3"
    store = SQLiteAttackGraphStore(path)
    await store.setup()
    assert await store.list_nodes() == []
    assert any(r.get(COLLECTION_KEY) == META_COLLECTION for r in _records(path))


async def test_hand_written_legacy_rows_without_header_fail_closed(tmp_path) -> None:
    path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(path) as db:
        db.executescript(
            """
            CREATE TABLE graph_records (
                collection TEXT NOT NULL,
                record_id TEXT NOT NULL,
                source_id TEXT,
                target_id TEXT,
                record TEXT NOT NULL,
                PRIMARY KEY (collection, record_id)
            );
            """
        )
        body = node("host:legacy").model_dump(mode="json")
        db.execute(
            "INSERT INTO graph_records(collection,record_id,record) VALUES(?,?,?)",
            ("nodes", "host:legacy", json.dumps({COLLECTION_KEY: "nodes", **body})),
        )
        db.commit()

    with pytest.raises(AttackGraphSchemaError):
        await SQLiteAttackGraphStore(path).setup()
