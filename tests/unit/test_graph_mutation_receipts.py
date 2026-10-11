"""Store receipts prove an original transaction, never current-value equality."""

import sqlite3

import pytest
from pydantic import ValidationError

from redteam.attack_graph.stores.base import StoreCapabilityError
from redteam.attack_graph.stores.memory import InMemoryAttackGraphStore
from redteam.attack_graph.stores.mutations import (
    GraphMutation,
    GraphMutationConflict,
    mutation_hash,
)
from redteam.attack_graph.stores.namespaced import EngagementScopedAttackGraphStore
from redteam.attack_graph.stores.sqlite import SQLiteAttackGraphStore
from tests.unit.test_attack_graph_sqlite_store import edge, node


def mutation(operation="add_node", *, mutation_id="original", run_id="run-original"):
    payload = {"node": node("host:a")} if operation == "add_node" else (
        {"edge": edge("edge:one", "host:a", "host:b")} if operation == "add_edge"
        else {"identity": "host:a" if operation == "remove_node" else "edge:one"}
    )
    return GraphMutation(run_id=run_id, mutation_id=mutation_id, operation=operation,
                         references=("graph", "node:host:a"), **payload)


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
@pytest.mark.parametrize("scoped", [False, True])
@pytest.mark.parametrize("operation", ["add_node", "add_edge", "remove_node", "remove_edge"])
async def test_atomic_receipt_survives_restart_and_duplicate_never_replays(
    tmp_path, backend, scoped, operation,
):
    path = tmp_path / "graph.sqlite3"
    inner = InMemoryAttackGraphStore() if backend == "memory" else SQLiteAttackGraphStore(path)
    store = EngagementScopedAttackGraphStore(inner, engagement_id="eng-a") if scoped else inner
    await store.add_node(node("host:a"))
    await store.add_node(node("host:b"))
    await store.add_edge(edge("edge:one", "host:a", "host:b"))
    request = await store.prepare_graph_mutation(mutation(operation))
    receipt = await store.apply_graph_mutation(request)
    assert receipt.request_hash == mutation_hash(request)
    assert receipt.result == (True if operation.startswith("remove_") else None)

    # A later legitimate mutation does not invalidate acknowledgement of the
    # original transaction. Re-submitting the original must not overwrite it.
    await store.add_node(node("host:a", labels=["later"]))
    await store.add_edge(edge("edge:one", "host:b", "host:a"))
    if backend == "sqlite":
        inner = SQLiteAttackGraphStore(path)
        store = EngagementScopedAttackGraphStore(inner, engagement_id="eng-a") if scoped else inner
    assert await store.read_graph_mutation_receipt("run-original", "original") == receipt
    assert await store.apply_graph_mutation(request) == receipt
    assert (await store.get_node("host:a")).labels == ["later"]
    assert (await store.get_edge("edge:one")).source_id == "host:b"
    assert await store.read_graph_mutation_receipt("different-run", "original") is None
    with pytest.raises(GraphMutationConflict, match="receipt_conflict"):
        await store.apply_graph_mutation(request.model_copy(update={"references": ("graph",)}))


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
@pytest.mark.parametrize("operation", ["remove_node", "remove_edge"])
async def test_negative_removal_receipt_is_noop_and_never_replays(tmp_path, backend, operation):
    store = InMemoryAttackGraphStore() if backend == "memory" else SQLiteAttackGraphStore(
        tmp_path / "graph.sqlite3",
    )
    request = await store.prepare_graph_mutation(mutation(operation))
    receipt = await store.apply_graph_mutation(request)
    assert receipt.result is False
    await store.add_node(node("host:a"))
    await store.add_edge(edge("edge:one", "host:a", "host:b"))
    assert await store.apply_graph_mutation(request) == receipt
    assert await store.get_node("host:a") and await store.get_edge("edge:one")


@pytest.mark.parametrize("operation", ["add_node", "add_edge", "remove_node", "remove_edge"])
async def test_sqlite_graph_and_receipt_roll_back_together(tmp_path, operation):
    path = tmp_path / "graph.sqlite3"
    store = SQLiteAttackGraphStore(path)
    await store.add_node(node("host:a", labels=["original"]))
    await store.add_edge(edge("edge:one", "host:a", "host:b"))
    before_nodes, before_edges = await store.list_nodes(), await store.list_edges()
    request = await store.prepare_graph_mutation(mutation(operation))
    with sqlite3.connect(path) as db:
        db.execute("""CREATE TRIGGER synthetic_receipt_failure
            BEFORE INSERT ON graph_mutation_receipts
            BEGIN SELECT RAISE(ABORT, 'synthetic storage failure'); END""")
    with pytest.raises(sqlite3.IntegrityError):
        await store.apply_graph_mutation(request)
    assert await store.read_graph_mutation_receipt(request.run_id, request.mutation_id) is None
    assert (await store.get_node("host:a")).labels == ["original"]
    assert (await SQLiteAttackGraphStore(path).get_node("host:a")).labels == ["original"]
    assert await store.list_nodes() == before_nodes and await store.list_edges() == before_edges


async def test_committed_receipt_survives_memory_mirror_ack_failure(tmp_path, monkeypatch):
    path = tmp_path / "graph.sqlite3"
    store = SQLiteAttackGraphStore(path)
    await store.setup()

    async def fail_mirror(*args):
        raise OSError("synthetic post-commit acknowledgement loss")

    monkeypatch.setattr(store._memory, "add_node", fail_mirror)
    with pytest.raises(OSError):
        await store.apply_graph_mutation(mutation())
    reopened = SQLiteAttackGraphStore(path)
    assert await reopened.read_graph_mutation_receipt("run-original", "original")
    assert await reopened.get_node("host:a") == node("host:a")
    assert await store.get_node("host:a") == node("host:a")


@pytest.mark.parametrize("damage", ["run_id", "mutation_id", "namespace", "schema_version"])
async def test_corrupt_receipt_index_is_not_a_valid_original(tmp_path, damage):
    path = tmp_path / "graph.sqlite3"
    store = SQLiteAttackGraphStore(path)
    await store.apply_graph_mutation(mutation())
    with sqlite3.connect(path) as db:
        db.execute("UPDATE graph_mutation_receipts SET receipt=json_set(receipt, ?, ?)",
                   (f"$.{damage}", 2 if damage == "schema_version" else "different"))
    with pytest.raises((GraphMutationConflict, ValidationError)):
        await SQLiteAttackGraphStore(path).read_graph_mutation_receipt("run-original", "original")


async def test_recovery_read_does_not_create_or_migrate_graph_database(tmp_path):
    path = tmp_path / "absent" / "graph.sqlite3"
    with pytest.raises(sqlite3.OperationalError):
        await SQLiteAttackGraphStore(path).read_graph_mutation_receipt("run-original", "original")
    assert not path.parent.exists()
    old = tmp_path / "old.sqlite3"
    with sqlite3.connect(old) as db:
        db.execute("CREATE TABLE graph_records(record TEXT)")
    with pytest.raises(sqlite3.OperationalError):
        await SQLiteAttackGraphStore(old).read_graph_mutation_receipt("run-original", "original")
    with sqlite3.connect(old) as db:
        found = db.execute(
            "SELECT name FROM sqlite_master WHERE name='graph_mutation_receipts'",
        ).fetchone()
        assert found is None


async def test_namespaces_never_cross_and_legacy_delete_keeps_original_behavior(tmp_path):
    inner = SQLiteAttackGraphStore(tmp_path / "graph.sqlite3")
    a = EngagementScopedAttackGraphStore(inner, engagement_id="eng-a")
    b = EngagementScopedAttackGraphStore(inner, engagement_id="eng-b")
    request = await a.prepare_graph_mutation(mutation())
    await a.apply_graph_mutation(request)
    assert await b.read_graph_mutation_receipt("run-original", "original") is None
    with pytest.raises(GraphMutationConflict, match="namespace_conflict"):
        await b.apply_graph_mutation(request)
    # Legacy physical edge ID compatibility is not rewritten by the new port.
    await inner.add_edge(edge("edge:one", "eng-a\x1fhost:a", "eng-a\x1fhost:b"))
    with pytest.raises(StoreCapabilityError, match="legacy_edge_removal_receipt"):
        await a.prepare_graph_mutation(mutation("remove_edge"))
    assert await a.remove_edge("edge:one") is True
    assert await a.get_edge("edge:one") is None


@pytest.mark.parametrize("operation", ["add_node", "add_edge", "remove_node", "remove_edge"])
def test_invalid_mutation_payload_rejected_before_storage(operation):
    with pytest.raises(ValidationError):
        GraphMutation(run_id="run", mutation_id="write", operation=operation,
                      references=("graph",))
