# Shared Attack Graph

Schemas: `redteam.core.contracts.graph`. Store:
`redteam.core.ports.attack_graph.AttackGraphStore` (in-memory
implementation: `redteam.attack_graph.stores.memory.InMemoryAttackGraphStore`).
Convenience API: `redteam.attack_graph.service.AttackGraphService`.

## Node types

```text
Host, Service, Endpoint, Principal, Credential, Asset, Resource, Hypothesis, Finding
```

`redteam.core.enums.GraphNodeType`

## Edge types

```text
HOSTS, EXPOSES, AUTHENTICATES_TO, CAN_ACCESS, OWNS, OBSERVED,
RELATED_TO, TARGETS, SUPPORTED_BY, DERIVED_FROM
```

`redteam.core.enums.GraphEdgeType`

## Schema

```python
class GraphNode(BaseModel):
    id: GraphNodeId
    type: GraphNodeType
    labels: list[str] = []
    properties: dict[str, Any] = {}
    created_at: datetime
    updated_at: datetime

class GraphEdge(BaseModel):
    id: GraphEdgeId
    type: GraphEdgeType
    source_id: GraphNodeId
    target_id: GraphNodeId
    properties: dict[str, Any] = {}
    created_at: datetime
```

Fields are `id`/`type` (not `node_id`/`node_type` or
`edge_id`/`edge_type`) — matches instruction.md section 16 exactly.
`Hypothesis` and `Finding` are separate lightweight models (also under
`redteam.core.contracts.graph`). **Current status**: `Hypothesis` nodes
are recorded from worker proposals grounded in preserved originals.
`Finding` nodes require the existing verification/promotion contract:
an explicit `graph.record` proposal passes origin and claim review, or a
verification result supports the tested hypothesis. Tool success alone does
not confirm a Finding or complete a Task/Mission. The default implementation
stores graph nodes, edges and mutation receipts in central `state.sqlite3`.
See [the current graph guide](../agent/attack-graph.md) for the retained APIs,
internal storage format and migration boundary.

## Store Protocol

```python
class AttackGraphStore(Protocol):
    async def add_node(self, node: GraphNode) -> None: ...
    async def add_edge(self, edge: GraphEdge) -> None: ...
    async def get_node(self, node_id: GraphNodeId) -> GraphNode | None: ...
    async def get_edge(self, edge_id: GraphEdgeId) -> GraphEdge | None: ...
    async def neighbors(self, node_id: GraphNodeId) -> list[GraphNode]: ...
    async def query(self, **filters: Any) -> list[GraphNode]: ...
```

## Why IDs, not embedded objects, in RuntimeState

`orchestration.state.RuntimeState.active_hypothesis_id` is a
`HypothesisId`, not a `Hypothesis`. The full object lives in the Attack
Graph and is fetched by ID when needed — this is what keeps LangGraph
state small and keeps the Attack Graph as the actual Source of Truth
for domain state (instruction.md section 5).

## Linking a TargetRef to a graph node

`ActionProposal.target` (a `TargetRef`, see `docs/contracts/actions.md`)
usually carries a `graph_node_id` once the target is known to the
graph. `redteam.core.contracts.target.target_graph_node_id(target)`
resolves it uniformly across every target kind — see
`orchestration.nodes.update.update_node` for the pattern.

## Swapping the backend

The optional central backend is selected by `build_central_graph(state)` after
initializing and binding `CentralStateStore`. It stores nodes, edges and existing
mutation receipts in `state.sqlite3`, reads SQL without a graph mirror, and
supports store-owned atomic node/edge updates and SQL `subgraph_around`.
Service/view/namespace wrappers forward these capabilities. See
[the central storage contract](central-state.md) for schema 2, shared commit,
cache policy and the snapshot/receipt format for later migration. Existing
default backend selection remains unchanged.

Neo4j or another graph DB is out of scope for this stage
(instruction.md section 21) but can be added later by implementing
`AttackGraphStore` and wiring it in `redteam.app.bootstrap.build_runtime_deps`.
