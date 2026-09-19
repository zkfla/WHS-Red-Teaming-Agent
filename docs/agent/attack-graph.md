# Shared Attack Graph — implementation

Code: `src/redteam/attack_graph/`. Schema: `core.contracts.graph`
(`GraphNode`/`GraphEdge`/`Hypothesis`/`Finding`). Port:
`core.ports.attack_graph.AttackGraphStore`. The contract side is
documented in `docs/contracts/attack-graph.md` — this page is what Agent
Team built on top of it.

```text
identity.py     stable "<type>:<key>" node ids + path normalization
predicates.py   the layout inside GraphNode.properties (+ secret guard)
patterns.py     preconditions: "which nodes could this action run against?"
observation.py  the bounded summary a planner sees
integrity.py    "is this graph safe to report from?"
snapshot.py     append-only JSONL export/import
view.py         read-only handle for components that must not write
service.py      the single write path
stores/         AttackGraphStore implementations
```

## Node identity

Every node id is `"<type>:<key>"` — `host:example.local`,
`endpoint:api.local GET /orders/{id}`. Two reasons:

- **Dedup.** `add_node` is keyed by id. With a fresh `new_id("node")`
  per observation the graph grows without bound and nothing merges.
- **Greppable evidence.** The id ends up in event payloads and log
  lines, where a random hex suffix says nothing.

Path normalization (`/orders/8831` -> `/orders/{id}`) exists for the
same reason: otherwise every request against an id-bearing URL mints a
new Endpoint node and precondition matching never converges.

`node_id_for_target(target)` derives an id from any `TargetRef` kind
(preferring an explicit `graph_node_id`), which closes the
"`target_graph_node_id(...)` can be `None`" gap the skeleton's UPDATE
node had to assert around. `target_for_node(...)` is the inverse, and
deliberately returns a concrete `HostTarget`/`ServiceTarget`/
`EndpointTarget` rather than wrapping the id in a `GraphNodeTarget` —
scope checking has no host-like identity to match on for `graph_node`
targets, so a wrapped target would be denied for the wrong reason.

## What goes in `properties`

`GraphNode.properties` is a free-form dict in the contract. Left
unstructured, every node ends up a different shape and nothing can match
on it. `predicates.py` fixes a small layout and is the only place
allowed to build or merge it:

```python
properties = {
    "attrs":       {"reachable": True, "requires_auth": False},  # predicates
    "facts":       {"banner": "nginx/1.25"},                     # observed values
    "provenance":  [{"step": 3, "source": "tool:...", "confidence": "measured"}],
    "retractions": [{"step": 5, "key": "funded", "reason": "balance spent"}],
    "first_seen_step": 3, "last_seen_step": 7,
}
```

- **attrs are predicates, facts are values.** Precondition matching only
  reads `attrs`, so it stays a cheap equality check instead of drifting
  into comparing measurements.
- **Observations accumulate.** `merge_properties` unions lists, keeps
  `True` sticky, ignores incoming `None`, and only moves
  `last_seen_step` forward. Re-observing never erases an earlier
  observation.
- **Predicates can be withdrawn, with a reason.** `retract(node, key,
  step, reason)` drops an attr and records why. Without it state only
  accumulates, and the planner keeps proposing actions whose
  preconditions stopped holding.
- **`Provenance.confidence` defaults to the weakest grade**
  (`inferred`). If a strong grade were the default, a value nobody
  thought about would silently become the sole basis for a finding.

**Secrets never enter the graph.** The graph is long-lived and gets
exported to the Evidence Store, so a leak here is unrecoverable.
`validate_attrs`/`validate_facts` reject secret-shaped key names
(`password`, `api_key`, `cvv`, ...) and PAN-shaped values outright, and
they run on the write path — not only where a caller remembers to ask.

The tool's output payload is deliberately *not* written to the graph.
The graph holds predicates and references; the payload is Evidence,
already stored by VERIFY and reachable by `EvidenceId`.

## Preconditions (`NodePattern` / `resolve`)

`resolve(patterns, nodes=..., edges=...)` answers "does the graph
currently contain nodes of this shape?" and returns either a binding or
a sentence saying why not. It backtracks rather than being greedy: an
early pattern taking the only viable candidate must not make a later one
fail spuriously.

Four fields carry most of the weight:

| Field | The failure it prevents |
|---|---|
| `from_param` | binding to *a* matching node instead of the node that was actually chosen — the node checked afterwards is then not the node acted on |
| `distinct_from` | two binds collapsing onto one node ("transferred to my own account, reported it as someone else's"). Symmetric: declaring it on one side binds both |
| `related_to` + `edge_type` / `edge_absent` | there is otherwise no way to state an authorization boundary ("an asset this principal does *not* own") as a precondition |
| `max_age_steps` | trusting an observation that is too old instead of re-observing |

Availability (`for_frontier=True`) ignores `from_param`, because at
frontier time the caller has not chosen anything yet. Commitment (the
default) enforces it.

## Observation

`build_observation` compresses the graph to a fixed size: counts, the
most recently seen nodes per type with their true predicates, open
hypotheses, findings, and what has already been attempted. It is a
transient projection — nothing reads it back as authority.

The "already attempted" list comes from `facts["attempted_operations"]`,
written by the UPDATE node. Deriving it from the graph rather than from
conversation history is what makes it survive a checkpoint restore and
stay consistent across agents in the same run.

## Hypotheses and findings

Both are stored as graph nodes (`hypothesis:<id>`, `finding:<id>`) with
the contract model in `facts`, linked to what they are about. A run's
whole chain — observation -> hypothesis -> action -> evidence -> finding
— is therefore one connected, exportable structure.

`record_finding` raises `UngroundedFindingError` for a `verified=True`
finding with no `evidence_ids`. By the time an unfalsifiable claim
reaches a report the run is over; write time is the last moment the
mistake is cheap.

## Integrity and snapshots

`check_integrity(nodes, edges)` returns findings, it does not raise —
the caller decides whether an unclean graph blocks reporting.
`report.blocking` separates the serious ones (ungrounded finding,
dangling edge, malformed id) from the informational ones (unsourced or
weakly-grounded node, open hypothesis).

`snapshot.to_records` / `from_records` are JSONL with a `_collection`
tag, matching how the Evidence Store already writes. An unknown
collection **raises** rather than being dropped: silently discarding a
record the reader does not understand means a report can go out missing
evidence with nobody aware of it. Export order is deterministic so
export -> import -> export is stable.

## Traversal and the evidence chain

`traversal.py` holds pure functions over materialized node/edge lists —
`walk`, `reachable_from`, `descendants`/`ancestors`, `shortest_path`,
`subgraph_around`. All are cycle-safe and bounded (`max_depth`,
`max_nodes`), because a pathological graph must not turn a report into a
hang.

`evidence_chain(finding)` is the one a report needs. It walks out from a
Finding over the edges VERIFY and PLAN actually write — `SUPPORTED_BY`
to the Hypothesis, `TARGETS` to what it was about — and collects the
`EvidenceId`/`ActionId`/`HypothesisId` stored alongside:

```text
find_1 [high] finding:find_1+verified --supported_by--> hypothesis:hyp_1
              --targets--> endpoint:web.local GET /admin+reachable
              (evidence: ev_42)
```

That is the difference between a finding a reviewer can retrace and an
assertion with no history. `service.evidence_chains()` returns every
chain, worst severity first.

## Withdrawing predicates that went stale

`predicates.apply_retraction` can withdraw a predicate, but something has
to notice it should be withdrawn. `retraction.py` is that: rules say
"`reachable` on a Host is only good for N steps", and `plan_retractions`
turns the current graph into the withdrawals that are due.

`service.sweep_stale_predicates(current_step=...)` applies them;
`dry_run=True` reports without writing. The OBSERVE node calls it once
per turn — that call is the only line of this feature living in
orchestration, and it is marked so the orchestration owner can move it.

Without the sweep, predicates only accumulate: the planner keeps being
offered steps whose preconditions stopped holding, and burns the budget
failing against them.

## Cleanup obligations

A test that creates accounts, cards or files and leaves them behind has
changed the target system and not said so. `cleanup.py` records the
obligation as state on the node itself (`properties["cleanup"]`), so it
survives a snapshot round trip:

```python
await service.register_created_resource(node_id, resource_type="user_account")
await service.mark_cleaned(node_id, evidence_id="ev_9")   # evidence required
```

`check_integrity` treats an outstanding item as **blocking** — a report
must not go out while the run is still holding something. Cleanup status
only ever moves forward under merge, so a later observation cannot reset
a cleaned resource back to pending.

The removal itself is an action, so calling it is Orchestration's job;
the graph only tracks what is owed.

## Rendering

`visualize.py` produces one self-contained HTML file — hand-rolled SVG,
no plotting library, no CDN, no script tag — so a report artifact opens
offline. Layout is deterministic (columns by node type, in discovery
order), so two runs can be compared side by side.

```python
await service.export_html("out/graph.html", title="Run 1")
await service.export_html("out/finding.html", around=node_id, radius=2)
```

Findings are outlined by severity; a resource still awaiting cleanup gets
a yellow outline. Node keys come from observed hostnames and paths, so
everything is HTML-escaped on the way in.

## Write permission

`AttackGraphService` is the write path. `AttackGraphView` is the
read-only façade with no `add_*`/`upsert`/`record_*`, and an unknown
attribute on it explains the boundary instead of raising a bare
`AttributeError`. Python cannot enforce this, but the handed-out type
expresses it: give a component the view and there is nothing to reach
for.

---

## Contract Change Proposal (not applied)

**Current contract.** `core.ports.attack_graph.AttackGraphStore`
(Contract v0.1) has `add_node`, `add_edge`, `get_node`, `get_edge`,
`neighbors`, `query(**filters) -> list[GraphNode]`. It cannot enumerate
edges, and cannot enumerate nodes except through `query`'s untyped
keyword filters.

**Problem.** Pattern resolution, observations, integrity checks and
snapshots all need the full node *and edge* sets. There is no
contract-level way to get edges at all.

**How it is handled today, without changing the contract.**
`attack_graph/stores/base.py` declares an Agent-Team-owned
`ListableAttackGraphStore` Protocol (`list_nodes`, `list_edges`) that a
store may additionally satisfy. `InMemoryAttackGraphStore` does. A store
that does not gets an explicit `StoreCapabilityError` naming the missing
capability, rather than a silently empty answer. Node-level operations
still work through the frozen port alone.

**Proposed change.** Fold the two methods into the core port:

```python
class AttackGraphStore(Protocol):
    ...
    async def list_nodes(self) -> list[GraphNode]: ...
    async def list_edges(self) -> list[GraphEdge]: ...
```

**Affected.** Agent Team (drop `stores/base.py`); any future non-Agent
store implementation. Additive for callers — breaking only for store
implementors, of which there is currently one.

**Alternative considered.** Keeping the split permanently. Workable, but
it leaves a capability every consumer needs outside the contract, so
each new backend has to rediscover that it must implement two Protocols.
