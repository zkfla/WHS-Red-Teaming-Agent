"""Cache-free graph rows in the central database, with a borrowed commit boundary.

Physical IDs retain the existing Engagement namespace encoding. Graph knowledge
is shared within that Engagement; mutation receipts retain the originating Run.
Only the outer CentralStateStore commits. No planning writer/fence is created.
"""

from __future__ import annotations

import inspect
import json
import sqlite3
from collections import deque
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, TypeVar

from redteam.attack_graph.identity import split_node_id
from redteam.attack_graph.predicates import validate_properties
from redteam.attack_graph.stores.mutations import (
    GraphMutation,
    GraphMutationConflict,
    GraphMutationReceipt,
    checked_receipt,
    make_receipt,
)
from redteam.attack_graph.traversal import DEFAULT_MAX_NODES, Direction
from redteam.attack_graph.writes import (
    EdgeUpdate,
    NodeUpdate,
    merge_edge,
    merge_node,
    validate_node,
)
from redteam.core.contracts.graph import GraphEdge, GraphNode
from redteam.core.enums import GraphNodeType
from redteam.core.ids import GraphEdgeId, GraphNodeId

if TYPE_CHECKING:
    from redteam.orchestration.central_state import CentralStateStore

T = TypeVar("T")

# Applied by CentralStateStore's 1 -> 2 upgrade on its own connection.
GRAPH_SCHEMA = (
    "CREATE TABLE state_graph_nodes (namespace TEXT NOT NULL, node_id TEXT NOT NULL, "
    "node_type TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(namespace,node_id))",
    "CREATE INDEX state_graph_node_type ON state_graph_nodes(namespace,node_type,node_id)",
    "CREATE TABLE state_graph_edges (namespace TEXT NOT NULL, edge_id TEXT NOT NULL, "
    "edge_type TEXT NOT NULL, source_id TEXT NOT NULL, target_id TEXT NOT NULL, "
    "payload TEXT NOT NULL, PRIMARY KEY(namespace,edge_id))",
    "CREATE INDEX state_graph_edge_source ON state_graph_edges(namespace,source_id,edge_id)",
    "CREATE INDEX state_graph_edge_target ON state_graph_edges(namespace,target_id,edge_id)",
    "CREATE TABLE state_graph_mutations (namespace TEXT NOT NULL, "
    "run_id TEXT NOT NULL REFERENCES state_runs(run_id), mutation_id TEXT NOT NULL, "
    "receipt TEXT NOT NULL, PRIMARY KEY(namespace,run_id,mutation_id))",
)


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _sync_result(value: T) -> T:
    if inspect.isawaitable(value):
        if inspect.iscoroutine(value):
            value.close()
        raise TypeError("graph row updates require synchronous callbacks without I/O")
    return value


class GraphTransaction:
    """Synchronous, scoped graph participant. It never commits the borrowed db."""

    def __init__(self, db: sqlite3.Connection, *, namespace: str, run_id: str) -> None:
        if not namespace or "\x1f" in namespace:
            raise ValueError("graph_namespace_invalid")
        self.db, self.namespace, self.run_id = db, namespace, run_id
        self._prefix = f"{namespace}\x1f"

    def _node_id(self, identity: str) -> None:
        if not identity.startswith(self._prefix):
            raise GraphMutationConflict("graph_node_namespace_conflict")
        split_node_id(GraphNodeId(identity[len(self._prefix):]))

    def check_node(self, node: GraphNode) -> GraphNode:
        node = validate_node(node)
        self._node_id(str(node.id))
        kind, _ = split_node_id(GraphNodeId(str(node.id)[len(self._prefix):]))
        if kind is not node.type:
            raise GraphMutationConflict("graph_node_type_identity_conflict")
        return node

    def check_edge(self, edge: GraphEdge) -> GraphEdge:
        edge = GraphEdge.model_validate(edge.model_dump())
        if not str(edge.id).startswith(self._prefix):
            raise GraphMutationConflict("graph_edge_namespace_conflict")
        self._node_id(str(edge.source_id))
        self._node_id(str(edge.target_id))
        validate_properties(edge.properties)
        return edge

    def get_node(self, node_id: GraphNodeId) -> GraphNode | None:
        # A wrapper's legacy ID probe must simply find no row.
        if not str(node_id).startswith(self._prefix):
            return None
        row = self.db.execute(
            "SELECT payload FROM state_graph_nodes WHERE namespace=? AND node_id=?",
            (self.namespace, str(node_id)),
        ).fetchone()
        if row is None:
            return None
        node = self.check_node(GraphNode.model_validate_json(row[0]))
        if node.id != node_id:
            raise GraphMutationConflict("graph_node_index_conflict")
        return node

    def get_edge(self, edge_id: GraphEdgeId) -> GraphEdge | None:
        if not str(edge_id).startswith(self._prefix):
            return None
        row = self.db.execute(
            "SELECT payload FROM state_graph_edges WHERE namespace=? AND edge_id=?",
            (self.namespace, str(edge_id)),
        ).fetchone()
        if row is None:
            return None
        edge = self.check_edge(GraphEdge.model_validate_json(row[0]))
        if edge.id != edge_id:
            raise GraphMutationConflict("graph_edge_index_conflict")
        return edge

    def get_local_node(self, node_id: GraphNodeId) -> GraphNode | None:
        """Read the same public ID as the engagement-scoped async graph port."""
        node = self.get_node(GraphNodeId(self._prefix + str(node_id)))
        return node.model_copy(update={"id": node_id}) if node is not None else None

    def get_local_edge(self, edge_id: GraphEdgeId) -> GraphEdge | None:
        """Read the same public ID as the engagement-scoped async graph port."""
        edge = self.get_edge(GraphEdgeId(self._prefix + str(edge_id)))
        if edge is None:
            return None
        return edge.model_copy(update={
            "id": edge_id,
            "source_id": GraphNodeId(str(edge.source_id)[len(self._prefix):]),
            "target_id": GraphNodeId(str(edge.target_id)[len(self._prefix):]),
        })

    def mutate_node(self, node_id: GraphNodeId, update: NodeUpdate) -> GraphNode | None:
        self._node_id(str(node_id))
        previous = self.get_node(node_id)
        result = _sync_result(update(previous.model_copy(deep=True) if previous else None))
        if result is None:
            return None
        result = self.check_node(result)
        if result.id != node_id or (previous is not None and result.type is not previous.type):
            raise GraphMutationConflict("graph_node_update_identity_conflict")
        if result != previous:
            self.db.execute(
                "INSERT INTO state_graph_nodes VALUES(?,?,?,?) ON CONFLICT(namespace,node_id) "
                "DO UPDATE SET payload=excluded.payload",
                (self.namespace, str(node_id), result.type.value,
                 _json(result.model_dump(mode="json"))),
            )
        return result

    def mutate_edge(self, edge_id: GraphEdgeId, update: EdgeUpdate) -> GraphEdge | None:
        previous = self.get_edge(edge_id)
        result = _sync_result(update(previous.model_copy(deep=True) if previous else None))
        if result is None:
            return None
        result = self.check_edge(result)
        if result.id != edge_id:
            raise GraphMutationConflict("graph_edge_update_identity_conflict")
        if previous is not None and (
            previous.type, previous.source_id, previous.target_id,
        ) != (result.type, result.source_id, result.target_id):
            raise GraphMutationConflict("graph_edge_update_identity_conflict")
        if result != previous:
            self.db.execute(
                "INSERT INTO state_graph_edges VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(namespace,edge_id) DO UPDATE SET payload=excluded.payload",
                (self.namespace, str(edge_id), result.type.value, str(result.source_id),
                 str(result.target_id), _json(result.model_dump(mode="json"))),
            )
        return result

    def add_node(self, node: GraphNode) -> None:
        node = self.check_node(node)
        self.mutate_node(node.id, lambda current: merge_node(current, node))

    def add_edge(self, edge: GraphEdge) -> None:
        edge = self.check_edge(edge)
        self.mutate_edge(edge.id, lambda current: merge_edge(current, edge))

    def query(self, **filters: Any) -> list[GraphNode]:
        node_type, label, attrs = (
            filters.pop("node_type", None), filters.pop("label", None), filters.pop("attrs", None),
        )
        if filters:
            raise TypeError(f"unsupported query filters: {sorted(filters)}")
        sql = "SELECT payload FROM state_graph_nodes n WHERE namespace=?"
        args: list[Any] = [self.namespace]
        if node_type is not None:
            sql += " AND node_type=?"
            args.append(GraphNodeType(node_type).value)
        if label is not None:
            sql += " AND EXISTS (SELECT 1 FROM json_each(n.payload,'$.labels') WHERE value=?)"
            args.append(label)
        for key, value in (attrs or {}).items():
            attribute = "SELECT 1 FROM json_each(n.payload,'$.properties.attrs') a WHERE a.key=?"
            if value is None:
                sql += f" AND NOT EXISTS ({attribute} AND a.value IS NOT NULL)"
                args.append(key)
            elif isinstance(value, (dict, list)):
                sql += f" AND EXISTS ({attribute} AND a.type IN ('object','array') AND a.value=?)"
                args.extend((key, _json(value)))
            else:
                sql += f" AND EXISTS ({attribute} AND a.value=?)"
                args.extend((key, value))
        return [self.check_node(GraphNode.model_validate_json(row[0]))
                for row in self.db.execute(sql + " ORDER BY node_id", args)]

    def list_nodes(self) -> list[GraphNode]:
        return self.query()

    def list_edges(self) -> list[GraphEdge]:
        return [self.check_edge(GraphEdge.model_validate_json(row[0])) for row in self.db.execute(
            "SELECT payload FROM state_graph_edges WHERE namespace=? ORDER BY edge_id",
            (self.namespace,),
        )]

    def incident_edges(
        self, node_id: GraphNodeId, *, direction: Direction = "any",
    ) -> list[GraphEdge]:
        if direction not in {"in", "out", "any"}:
            raise ValueError("graph_direction_invalid")
        # Separate indexed branches avoid scanning a whole namespace for OR.
        queries: list[str] = []
        args: list[str] = []
        for selected, column in (("out", "source_id"), ("in", "target_id")):
            if direction in {selected, "any"}:
                queries.append("SELECT edge_id,payload FROM state_graph_edges "
                               f"WHERE namespace=? AND {column}=?")
                args.extend((self.namespace, str(node_id)))
        return [self.check_edge(GraphEdge.model_validate_json(row[0])) for row in self.db.execute(
            "SELECT payload FROM (" + " UNION ".join(queries) + ") ORDER BY edge_id", args,
        )]

    def neighbors(self, node_id: GraphNodeId) -> list[GraphNode]:
        rows = self.db.execute(
            "SELECT payload FROM state_graph_nodes WHERE namespace=? AND node_id IN ("
            "SELECT target_id FROM state_graph_edges WHERE namespace=? AND source_id=? UNION "
            "SELECT source_id FROM state_graph_edges WHERE namespace=? AND target_id=?) "
            "ORDER BY node_id",
            (self.namespace, self.namespace, str(node_id), self.namespace, str(node_id)),
        )
        return [self.check_node(GraphNode.model_validate_json(row[0])) for row in rows]

    def subgraph_around(
        self, node_id: GraphNodeId, *, radius: int = 1, direction: Direction = "any",
    ) -> tuple[list[GraphNode], list[GraphEdge]]:
        if direction not in {"in", "out", "any"}:
            raise ValueError("graph_direction_invalid")
        kept = {node_id}
        queue = deque([(node_id, 0)])
        while queue and len(kept) < DEFAULT_MAX_NODES:
            current, depth = queue.popleft()
            if depth >= radius:
                continue
            for edge in self.incident_edges(current, direction=direction):
                endpoints = []
                if direction in {"out", "any"} and edge.source_id == current:
                    endpoints.append(edge.target_id)
                if direction in {"in", "any"} and edge.target_id == current:
                    endpoints.append(edge.source_id)
                for other in endpoints:
                    if other not in kept and len(kept) < DEFAULT_MAX_NODES:
                        kept.add(other)
                        queue.append((other, depth + 1))
        identities = _json(sorted(map(str, kept)))
        nodes = [self.check_node(GraphNode.model_validate_json(row[0])) for row in self.db.execute(
            "SELECT payload FROM state_graph_nodes WHERE namespace=? "
            "AND node_id IN (SELECT value FROM json_each(?)) ORDER BY node_id",
            (self.namespace, identities),
        )]
        edges = [self.check_edge(GraphEdge.model_validate_json(row[0])) for row in self.db.execute(
            "SELECT payload FROM state_graph_edges WHERE namespace=? "
            "AND source_id IN (SELECT value FROM json_each(?)) "
            "AND target_id IN (SELECT value FROM json_each(?)) ORDER BY edge_id",
            (self.namespace, identities, identities),
        )]
        return nodes, edges

    def remove_node(self, node_id: GraphNodeId) -> bool:
        self._node_id(str(node_id))
        removed = self.db.execute(
            "DELETE FROM state_graph_nodes WHERE namespace=? AND node_id=?",
            (self.namespace, str(node_id)),
        ).rowcount > 0
        if removed:
            self.db.execute(
                "DELETE FROM state_graph_edges WHERE namespace=? AND (source_id=? OR target_id=?)",
                (self.namespace, str(node_id), str(node_id)),
            )
        return removed

    def remove_edge(self, edge_id: GraphEdgeId) -> bool:
        if not str(edge_id).startswith(self._prefix):
            raise GraphMutationConflict("graph_edge_namespace_conflict")
        return self.db.execute(
            "DELETE FROM state_graph_edges WHERE namespace=? AND edge_id=?",
            (self.namespace, str(edge_id)),
        ).rowcount > 0

    def read_graph_mutation_receipt(
        self, run_id: str, mutation_id: str, *, namespace: str = "",
    ) -> GraphMutationReceipt | None:
        if run_id != self.run_id or namespace != self.namespace:
            raise GraphMutationConflict("graph_mutation_scope_conflict")
        row = self.db.execute(
            "SELECT receipt FROM state_graph_mutations "
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

    def apply_graph_mutation(self, mutation: GraphMutation) -> GraphMutationReceipt:
        mutation = GraphMutation.model_validate(mutation.model_dump()).model_copy(deep=True)
        previous = self.read_graph_mutation_receipt(
            mutation.run_id, mutation.mutation_id, namespace=mutation.namespace,
        )
        if previous is not None:
            return checked_receipt(previous, mutation)
        result: bool | None = None
        if mutation.node is not None:
            self.add_node(mutation.node)
        elif mutation.edge is not None:
            self.add_edge(mutation.edge)
        elif mutation.operation == "remove_node":
            assert mutation.identity is not None
            result = self.remove_node(GraphNodeId(mutation.identity))
        else:
            assert mutation.identity is not None
            result = self.remove_edge(GraphEdgeId(mutation.identity))
        receipt = make_receipt(mutation, result)
        self.db.execute("INSERT INTO state_graph_mutations VALUES(?,?,?,?)", (
            self.namespace, self.run_id, mutation.mutation_id, receipt.model_dump_json(),
        ))
        return receipt


class CentralAttackGraphStore:
    """Optional AttackGraphStore over an initialized, bound CentralStateStore.

    Use EngagementScopedAttackGraphStore at the application boundary. Every
    operation has a fresh SQLite snapshot; there is no persistent graph cache.
    """

    cacheable = False

    def __init__(self, state: CentralStateStore) -> None:
        self.state = state

    async def _change(
        self, operation: Callable[[GraphTransaction], T], *, write: bool = False,
    ) -> T:
        return await self.state.transaction(lambda tx: operation(tx.graph), write=write)

    async def add_node(self, node: GraphNode) -> None:
        await self._change(lambda graph: graph.add_node(node), write=True)

    async def add_edge(self, edge: GraphEdge) -> None:
        await self._change(lambda graph: graph.add_edge(edge), write=True)

    async def mutate_node(self, node_id: GraphNodeId, update: NodeUpdate) -> GraphNode | None:
        return await self._change(lambda graph: graph.mutate_node(node_id, update), write=True)

    async def mutate_edge(self, edge_id: GraphEdgeId, update: EdgeUpdate) -> GraphEdge | None:
        return await self._change(lambda graph: graph.mutate_edge(edge_id, update), write=True)

    async def get_node(self, node_id: GraphNodeId) -> GraphNode | None:
        return await self._change(lambda graph: graph.get_node(node_id))

    async def get_edge(self, edge_id: GraphEdgeId) -> GraphEdge | None:
        return await self._change(lambda graph: graph.get_edge(edge_id))

    async def neighbors(self, node_id: GraphNodeId) -> list[GraphNode]:
        return await self._change(lambda graph: graph.neighbors(node_id))

    async def incident_edges(
        self, node_id: GraphNodeId, *, direction: Direction = "any",
    ) -> list[GraphEdge]:
        return await self._change(lambda graph: graph.incident_edges(node_id, direction=direction))

    async def query(self, **filters: Any) -> list[GraphNode]:
        return await self._change(lambda graph: graph.query(**filters))

    async def list_nodes(self) -> list[GraphNode]:
        return await self._change(lambda graph: graph.list_nodes())

    async def list_edges(self) -> list[GraphEdge]:
        return await self._change(lambda graph: graph.list_edges())

    async def subgraph_around(
        self, node_id: GraphNodeId, *, radius: int = 1, direction: Direction = "any",
    ) -> tuple[list[GraphNode], list[GraphEdge]]:
        return await self._change(lambda graph: graph.subgraph_around(
            node_id, radius=radius, direction=direction,
        ))

    async def remove_node(self, node_id: GraphNodeId) -> bool:
        return await self._change(lambda graph: graph.remove_node(node_id), write=True)

    async def remove_edge(self, edge_id: GraphEdgeId) -> bool:
        return await self._change(lambda graph: graph.remove_edge(edge_id), write=True)

    async def prepare_graph_mutation(self, mutation: GraphMutation) -> GraphMutation:
        return GraphMutation.model_validate(mutation.model_dump()).model_copy(deep=True)

    async def apply_graph_mutation(self, mutation: GraphMutation) -> GraphMutationReceipt:
        mutation = await self.prepare_graph_mutation(mutation)
        return await self._change(lambda graph: graph.apply_graph_mutation(mutation), write=True)

    async def read_graph_mutation_receipt(
        self, run_id: str, mutation_id: str, *, namespace: str = "",
    ) -> GraphMutationReceipt | None:
        return await self._change(lambda graph: graph.read_graph_mutation_receipt(
            run_id, mutation_id, namespace=namespace,
        ))
