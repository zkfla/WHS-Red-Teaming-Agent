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
are real and produced in every run that proposes a step with a
`HypothesisDraft` (`AttackGraphService.record_hypothesis`, called from
`orchestration.nodes.plan`) — every built-in playbook step does.
`Finding` nodes are produced only when a step's `Expectation` reaches
`Verdict.VIOLATION` (not configured on built-in playbook steps — see
`docs/agent/orchestration-implementation-plan.md` section 16.1, finding
1), an explicit `graph.record` finding passes posthoc review plus
promotion (`orchestration.graph_recording`), or verification resolves a
tested hypothesis (`orchestration.nodes.verify`, `orchestration.recovery`).

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

Neo4j or another graph DB is out of scope for this stage
(instruction.md section 21) but can be added later by implementing
`AttackGraphStore` and wiring it in `redteam.app.bootstrap.build_runtime_deps`.
