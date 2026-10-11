"""Durable SQLite ``AttackGraphStore`` for process-restart recovery.

The Attack Graph is the Source of Truth for domain state
(``docs/contracts/attack-graph.md`` "Why IDs, not embedded objects, in
RuntimeState"). The default ``InMemoryAttackGraphStore`` loses it the
moment the process exits, so a *separate* ``redteam status``/``findings``/
``report`` process has nothing to read and a crashed run cannot resume
the graph it had built. This store makes that graph durable without
changing the frozen ``AttackGraphStore`` port.

Design
------
* **Same serialization as snapshots.** Every row stores one
  ``attack_graph.snapshot`` record exactly as ``to_records`` writes it
  (``_collection`` tag + ``model_dump(mode="json")`` body). On startup the
  rows are handed to ``snapshot.from_records``, which is where the
  schema-version check already lives — so this store inherits the same
  "refuse to read what I do not understand" guarantee rather than
  re-inventing it.
* **Fail closed.** A database whose ``graph_meta`` record is missing (but
  which has data) or carries an unsupported ``schema_version`` raises
  ``AttackGraphSchemaError`` (a ``SnapshotError``) at ``setup()``; it never
  loads with a best-effort interpretation.
* **Tenancy is preserved.** Ids are stored verbatim, including the
  ``\\x1f`` namespace separator ``EngagementScopedAttackGraphStore`` puts
  on them, so a single database shared by several engagements keeps
  working through that wrapper across restarts. This store never strips,
  rewrites or interprets an id.
* **Restart-safe writes.** Nodes and edges are persisted one row at a
  time inside an explicit transaction (``BEGIN IMMEDIATE`` + commit), not
  by rewriting the whole graph. A crash mid-write rolls back and leaves
  the previous consistent state; a failed write marks the store not-ready
  so the next call reloads from disk instead of trusting an in-memory
  change that never landed.
* **Identical read semantics.** Reads and ``query``/``neighbors`` delegate
  to an ``InMemoryAttackGraphStore`` refreshed from disk, so callers see
  the same detached copies, adjacency and filter behavior as the default
  store.

This implements the core ``AttackGraphStore`` Protocol plus the optional
``ListableAttackGraphStore``/``MutableAttackGraphStore`` Protocols.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from redteam.attack_graph.snapshot import (
    COLLECTION_KEY,
    EDGES_COLLECTION,
    META_COLLECTION,
    NODES_COLLECTION,
    SNAPSHOT_SCHEMA_VERSION,
    SnapshotError,
    from_records,
)
from redteam.attack_graph.stores.memory import InMemoryAttackGraphStore
from redteam.attack_graph.stores.mutations import (
    GraphMutation,
    GraphMutationConflict,
    GraphMutationReceipt,
    checked_receipt,
    make_receipt,
    mutation_hash,
)
from redteam.core.contracts.graph import GraphEdge, GraphNode
from redteam.core.ids import GraphEdgeId, GraphNodeId


class AttackGraphSchemaError(SnapshotError):
    """The on-disk graph cannot be read without losing information.

    Raised when the schema header is missing while data is present, or
    when its ``schema_version`` is not in
    ``snapshot.SUPPORTED_SCHEMA_VERSIONS``. A ``SnapshotError`` subclass so
    existing snapshot error handling also covers the durable store.
    """


def _meta_record() -> str:
    return json.dumps(
        {COLLECTION_KEY: META_COLLECTION, "schema_version": SNAPSHOT_SCHEMA_VERSION},
        sort_keys=True,
        separators=(",", ":"),
    )


def _node_record(node: GraphNode) -> str:
    return json.dumps(
        {COLLECTION_KEY: NODES_COLLECTION, **node.model_dump(mode="json")},
        sort_keys=True,
        separators=(",", ":"),
    )


def _edge_record(edge: GraphEdge) -> str:
    return json.dumps(
        {COLLECTION_KEY: EDGES_COLLECTION, **edge.model_dump(mode="json")},
        sort_keys=True,
        separators=(",", ":"),
    )


class SQLiteAttackGraphStore:
    """One ``AttackGraphStore`` per SQLite file.

    The file may be shared across engagements only through
    ``EngagementScopedAttackGraphStore`` — a raw instance hands every
    caller the same un-namespaced graph, exactly like
    ``InMemoryAttackGraphStore``.
    """

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._memory = InMemoryAttackGraphStore()
        self._ready = False
        self._lock = asyncio.Lock()

    # -- lifecycle ------------------------------------------------------

    async def setup(self) -> None:
        """Create the file if needed and load it, or raise ``AttackGraphSchemaError``.

        Idempotent and lazy: every method calls it, so a fresh process
        recovers the durable graph on its first operation. A failed load
        leaves the store not-ready, so a later call retries instead of
        serving a half-loaded graph.
        """
        if self._ready:
            return
        async with self._lock:
            if self._ready:
                return
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with closing(sqlite3.connect(self._path)) as db:
                self._ensure_schema(db)
                records = self._read_records(db)
                memory = InMemoryAttackGraphStore()
                try:
                    nodes, edges, _ = from_records(records)
                except (SnapshotError, ValueError) as exc:
                    raise AttackGraphSchemaError(
                        f"cannot read Attack Graph from {self._path!s}: {exc}"
                    ) from exc
                for node in nodes:
                    await memory.add_node(node)
                for edge in edges:
                    await memory.add_edge(edge)
                if not self._has_meta(db):
                    # Fresh, empty database: stamp it with this writer's
                    # schema version so a newer reader can fail closed.
                    db.execute(
                        "INSERT INTO graph_records(collection,record_id,record) VALUES(?,?,?)",
                        (META_COLLECTION, "", _meta_record()),
                    )
                    db.commit()
            self._memory = memory
            self._ready = True

    @staticmethod
    def _ensure_schema(db: sqlite3.Connection) -> None:
        db.execute("PRAGMA journal_mode=WAL")
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS graph_records (
                collection TEXT NOT NULL,
                record_id TEXT NOT NULL,
                source_id TEXT,
                target_id TEXT,
                record TEXT NOT NULL,
                PRIMARY KEY (collection, record_id)
            );
            CREATE INDEX IF NOT EXISTS idx_graph_edges_source
                ON graph_records(source_id) WHERE collection='edges';
            CREATE INDEX IF NOT EXISTS idx_graph_edges_target
                ON graph_records(target_id) WHERE collection='edges';
            CREATE TABLE IF NOT EXISTS graph_mutation_receipts (
                namespace TEXT NOT NULL,
                run_id TEXT NOT NULL,
                mutation_id TEXT NOT NULL,
                receipt TEXT NOT NULL,
                PRIMARY KEY (namespace, run_id, mutation_id)
            );
            """
        )

    @staticmethod
    def _has_meta(db: sqlite3.Connection) -> bool:
        row = db.execute(
            "SELECT 1 FROM graph_records WHERE collection=? LIMIT 1", (META_COLLECTION,)
        ).fetchone()
        return row is not None

    def _read_records(self, db: sqlite3.Connection) -> list[dict[str, Any]]:
        rows = db.execute(
            "SELECT record FROM graph_records "
            "ORDER BY CASE collection WHEN 'graph_meta' THEN 0 "
            "WHEN 'nodes' THEN 1 ELSE 2 END, record_id"
        ).fetchall()
        return [json.loads(row[0]) for row in rows]

    # -- persistence ----------------------------------------------------

    async def prepare_graph_mutation(self, mutation: GraphMutation) -> GraphMutation:
        return GraphMutation.model_validate(mutation.model_dump()).model_copy(deep=True)

    @staticmethod
    def _read_receipt(db, run_id, mutation_id, namespace) -> GraphMutationReceipt | None:
        row = db.execute(
            "SELECT receipt FROM graph_mutation_receipts "
            "WHERE namespace=? AND run_id=? AND mutation_id=?",
            (namespace, run_id, mutation_id),
        ).fetchone()
        if row is None:
            return None
        receipt = GraphMutationReceipt.model_validate_json(row[0])
        if (receipt.namespace, receipt.run_id, receipt.mutation_id) != (
            namespace, run_id, mutation_id,
        ):
            raise GraphMutationConflict("graph_mutation_receipt_index_conflict")
        return receipt

    async def read_graph_mutation_receipt(
        self, run_id: str, mutation_id: str, *, namespace: str = "",
    ) -> GraphMutationReceipt | None:
        # Recovery reads do NOT set up, migrate, create, or rewrite the graph.
        uri = f"{self._path.resolve().as_uri()}?mode=ro"
        with closing(sqlite3.connect(uri, uri=True)) as db:
            return self._read_receipt(db, run_id, mutation_id, namespace)

    async def apply_graph_mutation(self, mutation: GraphMutation) -> GraphMutationReceipt:
        mutation = await self.prepare_graph_mutation(mutation)
        mutation_hash(mutation)
        await self.setup()
        async with self._lock:
            try:
                with closing(sqlite3.connect(self._path)) as db:
                    db.execute("BEGIN IMMEDIATE")
                    previous = self._read_receipt(
                        db, mutation.run_id, mutation.mutation_id, mutation.namespace,
                    )
                    if previous is not None:
                        return checked_receipt(previous, mutation)
                    statements, result = self._mutation_statements(db, mutation)
                    receipt = make_receipt(mutation, result)
                    for sql, params in statements:
                        db.execute(sql, params)
                    db.execute(
                        "INSERT INTO graph_mutation_receipts VALUES(?,?,?,?)",
                        (mutation.namespace, mutation.run_id, mutation.mutation_id,
                         receipt.model_dump_json()),
                    )
                    db.commit()
                # Concrete memory mirror methods never suspend. On any failure
                # after commit, disk+receipt remain canonical and setup reloads.
                if mutation.node is not None:
                    await self._memory.add_node(mutation.node)
                elif mutation.edge is not None:
                    await self._memory.add_edge(mutation.edge)
                elif mutation.operation == "remove_node":
                    assert mutation.identity is not None
                    await self._memory.remove_node(GraphNodeId(mutation.identity))
                else:
                    assert mutation.identity is not None
                    await self._memory.remove_edge(GraphEdgeId(mutation.identity))
                return receipt
            except BaseException:
                self._ready = False
                raise

    def _mutation_statements(self, db, mutation):
        if mutation.node is not None:
            return self._node_statements(mutation.node), None
        if mutation.edge is not None:
            return self._edge_statements(mutation.edge), None
        collection = NODES_COLLECTION if mutation.operation == "remove_node" else EDGES_COLLECTION
        removed = db.execute(
            "SELECT 1 FROM graph_records WHERE collection=? AND record_id=?",
            (collection, mutation.identity),
        ).fetchone() is not None
        statements = []
        if removed:
            statements = (self._node_removal_statements(mutation.identity)
                          if mutation.operation == "remove_node"
                          else self._edge_removal_statements(mutation.identity))
        return statements, removed

    def _write(self, statements: list[tuple[str, tuple[Any, ...]]]) -> None:
        with closing(sqlite3.connect(self._path)) as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                for sql, params in statements:
                    db.execute(sql, params)
                db.commit()
            except Exception:
                db.rollback()
                raise

    def _persist_node(self, node: GraphNode) -> None:
        self._write(self._node_statements(node))

    @staticmethod
    def _node_statements(node):
        return [
            (
                "INSERT OR REPLACE INTO graph_records"
                "(collection,record_id,source_id,target_id,record) VALUES(?,?,NULL,NULL,?)",
                (NODES_COLLECTION, str(node.id), _node_record(node)),
            )
        ]

    def _persist_edge(self, edge: GraphEdge) -> None:
        self._write(self._edge_statements(edge))

    @staticmethod
    def _edge_statements(edge):
        return [
            (
                "INSERT OR REPLACE INTO graph_records"
                "(collection,record_id,source_id,target_id,record) VALUES(?,?,?,?,?)",
                (
                    EDGES_COLLECTION,
                    str(edge.id),
                    str(edge.source_id),
                    str(edge.target_id),
                    _edge_record(edge),
                ),
            )
        ]

    def _persist_node_removal(self, node_id: GraphNodeId) -> None:
        self._write(self._node_removal_statements(node_id))

    @staticmethod
    def _node_removal_statements(node_id):
        return [
            (
                "DELETE FROM graph_records WHERE collection=? "
                "AND (source_id=? OR target_id=?)",
                (EDGES_COLLECTION, str(node_id), str(node_id)),
            ),
            (
                "DELETE FROM graph_records WHERE collection=? AND record_id=?",
                (NODES_COLLECTION, str(node_id)),
            ),
        ]

    def _persist_edge_removal(self, edge_id: GraphEdgeId) -> None:
        self._write(self._edge_removal_statements(edge_id))

    @staticmethod
    def _edge_removal_statements(edge_id):
        return [
            (
                "DELETE FROM graph_records WHERE collection=? AND record_id=?",
                (EDGES_COLLECTION, str(edge_id)),
            )
        ]

    # -- AttackGraphStore (core port) -----------------------------------

    async def add_node(self, node: GraphNode) -> None:
        await self.setup()
        async with self._lock:
            await self._memory.add_node(node)
            self._persist_or_invalidate(self._persist_node, node)

    async def add_edge(self, edge: GraphEdge) -> None:
        await self.setup()
        async with self._lock:
            await self._memory.add_edge(edge)
            self._persist_or_invalidate(self._persist_edge, edge)

    async def get_node(self, node_id: GraphNodeId) -> GraphNode | None:
        await self.setup()
        return await self._memory.get_node(node_id)

    async def get_edge(self, edge_id: GraphEdgeId) -> GraphEdge | None:
        await self.setup()
        return await self._memory.get_edge(edge_id)

    async def neighbors(self, node_id: GraphNodeId) -> list[GraphNode]:
        await self.setup()
        return await self._memory.neighbors(node_id)

    async def incident_edges(
        self, node_id: GraphNodeId, *, direction: str = "any"
    ) -> list[GraphEdge]:
        await self.setup()
        return await self._memory.incident_edges(node_id, direction=direction)

    async def query(self, **filters: Any) -> list[GraphNode]:
        await self.setup()
        return await self._memory.query(**filters)

    # -- ListableAttackGraphStore ---------------------------------------

    async def list_nodes(self) -> list[GraphNode]:
        await self.setup()
        return await self._memory.list_nodes()

    async def list_edges(self) -> list[GraphEdge]:
        await self.setup()
        return await self._memory.list_edges()

    # -- MutableAttackGraphStore ----------------------------------------

    async def remove_node(self, node_id: GraphNodeId) -> bool:
        await self.setup()
        async with self._lock:
            removed = await self._memory.remove_node(node_id)
            if removed:
                self._persist_or_invalidate(self._persist_node_removal, node_id)
            return removed

    async def remove_edge(self, edge_id: GraphEdgeId) -> bool:
        await self.setup()
        async with self._lock:
            removed = await self._memory.remove_edge(edge_id)
            if removed:
                self._persist_or_invalidate(self._persist_edge_removal, edge_id)
            return removed

    # -- failure handling ------------------------------------------------

    def _persist_or_invalidate(self, persist: Any, *args: Any) -> None:
        """Run ``persist``; on failure drop the in-memory mirror.

        Disk stays at the previous committed state and is authoritative,
        so the next ``setup()`` reloads it rather than continuing from an
        in-memory change that never landed.
        """
        try:
            persist(*args)
        except Exception:
            self._ready = False
            raise
