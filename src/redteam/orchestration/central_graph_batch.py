"""Prepare result interpretation outside SQL; commit only its touched graph rows.

This is a disposable action write set, never another domain store. The original
graph stays SQL-backed; unrelated rows do not invalidate the prepared changes.
Evidence and model I/O happen before the borrowed central commit boundary.
"""

from redteam.attack_graph.stores.mutations import GraphMutation
from redteam.attack_graph.writes import merge_edge, merge_node
from redteam.core.ids import GraphEdgeId, GraphNodeId
from redteam.orchestration.coordination import StateConflict, content_hash


class CentralGraphBatch:
    cacheable = False

    def __init__(self, source, state) -> None:
        self.source, self.state = source, state
        self.nodes: dict = {}
        self.edges: dict = {}
        self.original_nodes: dict = {}
        self.original_edges: dict = {}
        # Store-owned synchronous row updates can be applied to the latest SQL
        # row. The callbacks contain no evidence/model/provider I/O.
        self.node_updates: dict = {}
        self.edge_updates: dict = {}
        self.events: list = []
        self.receipts: list = []

    async def get_node(self, identity):
        if identity not in self.nodes:
            value = await self.source.get_node(identity)
            self.original_nodes.setdefault(identity, value)
            return value.model_copy(deep=True) if value is not None else None
        value = self.nodes[identity]
        return value.model_copy(deep=True) if value is not None else None

    async def get_edge(self, identity):
        if identity not in self.edges:
            value = await self.source.get_edge(identity)
            self.original_edges.setdefault(identity, value)
            return value.model_copy(deep=True) if value is not None else None
        value = self.edges[identity]
        return value.model_copy(deep=True) if value is not None else None

    async def mutate_node(self, identity, update):
        value = update(await self.get_node(identity))
        if value is not None:
            self.nodes[identity] = value.model_copy(deep=True)
            self.node_updates.setdefault(identity, []).append(update)
        return value

    async def mutate_edge(self, identity, update):
        value = update(await self.get_edge(identity))
        if value is not None:
            self.edges[identity] = value.model_copy(deep=True)
            self.edge_updates.setdefault(identity, []).append(update)
        return value

    async def add_node(self, node):
        await self.mutate_node(node.id, lambda old: merge_node(old, node))

    async def add_edge(self, edge):
        await self.mutate_edge(edge.id, lambda old: merge_edge(old, edge))

    async def list_nodes(self, *, node_type=None):
        current = {
            n.id: n
            for n in await self.source.list_nodes()
            if node_type is None or n.type == node_type
        }
        for identity, node in self.nodes.items():
            current.pop(identity, None)
            if node is not None and (node_type is None or node.type == node_type):
                current[identity] = node.model_copy(deep=True)
        return list(current.values())

    async def list_edges(self, *, source_id=None, target_id=None):
        current = {
            e.id: e
            for e in await self.source.list_edges()
            if (source_id is None or e.source_id == source_id)
            and (target_id is None or e.target_id == target_id)
        }
        for identity, edge in self.edges.items():
            current.pop(identity, None)
            if (
                edge is not None
                and (source_id is None or edge.source_id == source_id)
                and (target_id is None or edge.target_id == target_id)
            ):
                current[identity] = edge.model_copy(deep=True)
        return list(current.values())

    async def query(self, **filters):
        from redteam.attack_graph.stores.memory import InMemoryAttackGraphStore

        view = InMemoryAttackGraphStore()
        for node in await self.list_nodes():
            await view.add_node(node)
        return await view.query(**filters)

    async def neighbors(self, identity):
        identities = {
            e.target_id if e.source_id == identity else e.source_id
            for e in await self.list_edges()
            if identity in (e.source_id, e.target_id)
        }
        nodes = [await self.get_node(i) for i in identities]
        return [n for n in nodes if n is not None]

    async def remove_node(self, identity):
        present = await self.get_node(identity) is not None
        self.nodes[identity] = None
        for edge in await self.list_edges():
            if identity in (edge.source_id, edge.target_id):
                await self.remove_edge(edge.id)
        return present

    async def remove_edge(self, identity):
        present = await self.get_edge(identity) is not None
        self.edges[identity] = None
        return present

    async def prepare_graph_mutation(self, mutation):
        return await self.source.prepare_graph_mutation(mutation)

    async def apply_graph_mutation(self, mutation):
        # Recording consumers require an acknowledgment. It becomes durable
        # together with the graph and execution in commit(), never earlier.
        from redteam.attack_graph.stores.mutations import make_receipt

        if mutation.namespace != self.state.engagement_id:
            raise StateConflict("graph_batch_namespace_mismatch")
        prior = await self.source.read_graph_mutation_receipt(
            mutation.run_id,
            mutation.mutation_id,
            namespace=mutation.namespace,
        )
        if prior is not None:
            from redteam.attack_graph.stores.mutations import checked_receipt

            return checked_receipt(prior, mutation)
        result = None
        if mutation.node is not None:
            await self.add_node(mutation.node)
        elif mutation.edge is not None:
            await self.add_edge(mutation.edge)
        elif mutation.operation == "remove_node":
            result = await self.remove_node(GraphNodeId(mutation.identity))
        else:
            result = await self.remove_edge(GraphEdgeId(mutation.identity))
        self.receipts.append((mutation, make_receipt(mutation, result)))
        return self.receipts[-1][1]

    async def read_graph_mutation_receipt(self, *args, **kwargs):
        return await self.source.read_graph_mutation_receipt(*args, **kwargs)

    async def publish(self, event):
        self.events.append(event)

    async def subscribe(self, event_type, handler):
        raise ValueError("result_batch_is_not_an_event_subscription_boundary")

    def commit(self, tx, prepared_nodes, prepared_edges):
        graph = tx.graph
        prepared_rows = []
        # Check the read set before own writes/cascading deletes can change it.
        for original, prepared in [*prepared_nodes, *prepared_edges]:
            node = isinstance(prepared, GraphMutation) and prepared.operation in {
                "add_node",
                "remove_node",
            }
            identity = (
                prepared.node.id
                if prepared.node
                else (prepared.edge.id if prepared.edge else prepared.identity)
            )
            current = graph.get_node(identity) if node else graph.get_edge(identity)
            if content_hash(current) != content_hash(original):
                callbacks = (self.node_updates if node else self.edge_updates).get(identity, [])
                if (
                    prepared.operation not in {"add_node", "add_edge"}
                    or not callbacks
                    or (original is not None and current is None)
                ):
                    raise StateConflict("related_graph_row_changed")
                # Preserve sibling observations using the existing atomic row
                # update contract, rather than rewriting a stale full snapshot.
                value = current.model_copy(deep=True) if current is not None else None
                for update in callbacks:
                    value = update(value)
                    if value is None:
                        raise StateConflict("related_graph_update_no_longer_applicable")
                prepared = prepared.model_copy(update={"node" if node else "edge": value})
            prepared_rows.append((original, prepared))
        order = {"remove_edge": 0, "remove_node": 1, "add_node": 2, "add_edge": 3}
        for _, prepared in sorted(prepared_rows, key=lambda row: order[row[1].operation]):
            graph.apply_graph_mutation(prepared)
        # Explicit caller mutations retain their original immutable hash.
        for mutation, receipt in self.receipts:
            previous = graph.read_graph_mutation_receipt(
                mutation.run_id,
                mutation.mutation_id,
                namespace=mutation.namespace,
            )
            if previous is not None and previous != receipt:
                raise StateConflict("graph_batch_receipt_conflict")
            if previous is None:
                tx.db.execute(
                    "INSERT INTO state_graph_mutations VALUES(?,?,?,?)",
                    (
                        mutation.namespace,
                        mutation.run_id,
                        mutation.mutation_id,
                        receipt.model_dump_json(),
                    ),
                )
        for event in self.events:
            tx.queue(event)

    async def prepare(self, execution_id):
        # Use the original wrapper's namespace preparation, without graph I/O in SQL.
        nodes, edges = [], []
        for kind, changes, originals, destination in (
            ("node", self.nodes, self.original_nodes, nodes),
            ("edge", self.edges, self.original_edges, edges),
        ):
            for identity, value in changes.items():
                original = originals.get(identity)
                if value == original:
                    continue
                mutation = GraphMutation(
                    run_id=self.state.run_id,
                    mutation_id=f"result:{execution_id}:{kind}:{identity}",
                    operation=f"add_{kind}" if value is not None else f"remove_{kind}",
                    namespace=self.state.engagement_id,
                    node=value if kind == "node" else None,
                    edge=value if kind == "edge" else None,
                    identity=str(identity) if value is None else None,
                    references=("graph",),
                )
                prepared = await self.source.prepare_graph_mutation(mutation)
                # Public namespace encoding is provided by the selected wrapper.
                original_prepared = None
                if original is not None:
                    original_mutation = mutation.model_copy(
                        update={
                            "operation": f"add_{kind}",
                            "identity": None,
                            "node": original if kind == "node" else None,
                            "edge": original if kind == "edge" else None,
                        }
                    )
                    encoded = await self.source.prepare_graph_mutation(original_mutation)
                    original_prepared = encoded.node or encoded.edge
                destination.append((original_prepared, prepared))
        return nodes, edges
