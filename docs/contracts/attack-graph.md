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

Version-3 and version-4 posthoc review receipts may retain typed, reviewer-adopted Finding
detail bound to the exact Task result ID, draft ID and normalized draft hash.
Version 4 additionally carries the reviewer's bounded `confidence`. A supported
security review promotes only at `confidence >= 0.80`; lower confidence leaves
the candidate inconclusive. Historical version-2/3 receipts retain their stored
categorical semantics and are never rewritten.
Central candidate admission rechecks that stored draft before promotion. The
shared promoter then uses the adopted title, summary and severity and stores the
complete classification, claim, impact, limitations, evidence roles and lineage
in Finding metadata. Historical/v1/mapper Findings keep their existing fallback
title, description and severity. Read-only JSON/Markdown reports expose typed
detail without borrowing current Task state.

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
## Revisioned investigation candidates (plan5a)

Candidate hypotheses use `Hypothesis.contract_version=2` in the existing
Engagement-scoped graph. The shared fields include target/principal references,
premise, supporting/refuting original evidence references, source hypotheses,
current judgment reason and a code-owned revision. Status values are `open`,
`held`, `inconclusive`, `confirmed`, and `rejected` (a refuted candidate). Confidence
alone grants no verdict. Active planner candidate updates use a v2-only output
contract; the schema and runtime validation require the same version and fields.
The legacy v1 implementation is retained for inactive historical code paths,
but is not an active planner output format. This prototype does not promise v1
Run migration or resume compatibility.

New candidates expect absence; updates expect the current revision. Existing
v1-upgrade code remains retained and is not the normal planner workflow.
Accepted writes increment the revision, replace current candidate facts independently
of worker-local steps, retain original evidence references and record TARGETS
relations. Source hypotheses preserve candidate lineage without imposing a DAG.
The existing knowledge projection exposes these typed references and revisions.

Ordinary graph upsert/merge and legacy `record_hypothesis`/`resolve_hypothesis`
cannot overwrite managed candidate facts. Writes go through central candidate/probe
admission, checking the relevant read set and references. Generic deletion also
retains managed candidate identities; a closed/held judgment remains readable.
The storage participant
does not commit independently. Confirmed/rejected judgments require the existing
independent original-result/claim/validity proof plus a final public review receipt
bound to `candidate_revision` and `candidate_purpose_hash` (claim, premise, targets
and principals). `append_posthoc_claim_receipt(candidate=...)` binds the reviewed
snapshot. The Run-bound central review applicator consumes that final receipt
without asking Planner to choose the verdict again. Version-3/4 reviews created
before Task settlement remain explicitly `pending_task_result`; canonical
`task.settle` then applies the stored draft binding. Candidate transition,
Finding, promotion/application projections and outbox event share one SQLite
transaction. Planner consumes the result for follow-up questions only.

Only a versioned security review
with `success`, `VIOLATION` and `claim_supported=true` is eligible for the
shared `FindingPromoter`; version-4 review confidence must also be at least
`0.80`. The
resulting verified Finding links both the receipt
and its original execution evidence. The Finding stores the reviewer-adopted
title, statement/summary, impact, severity basis and limitations. `verified`
means this independent semantic decision passed the code-owned structural
binding gates; code does not parse payloads or response prose to rediscover the
vulnerability class. A Run/candidate/target/claim identity plus legacy claim-key
alias makes replay and old Planner-confirmed v2 Findings deduplicate. Refuted
reviews reject the candidate without a Finding. Inconclusive, pending and
refused applications remain distinct stored states. Worker observations, failed
requests, mock results, missing prerequisites and UNKNOWN do not create a final
verdict.

The integrity report treats v2 held/inconclusive candidates as unresolved. Goal
completion only requires the candidates explicitly named by v2 goal-required
contracts; historical v1 task semantics remain unchanged.
