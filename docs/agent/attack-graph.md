# Current Attack Graph

The graph represents security observations, hypotheses, Findings, relationships
and cleanup obligations. Default storage is the existing graph model in central
SQLite, alongside investigation/execution state. This does not turn every Task
or result question into a graph node or add a new relation graph/context API.

## Kept contracts

`core/contracts/graph.py` defines nodes, edges, hypotheses and Findings.
`core/ports/attack_graph.py` defines the shared store port. `AttackGraphService`
owns validated writes and `AttackGraphView` provides a read-only handle.

Retained operations include node/edge lookup, neighbors, filters, list/count,
observation projection, integrity/cleanup reads and `subgraph_around`.
The latter is used by context and storage readers; its bounded traversal and
backend implementation remain. Generic ancestor/descendant/shortest-path and
rendered evidence-chain APIs have no application/registry/report consumer and
were removed. Reporting verifies actual evidence/claim origins directly.
The unused HTML/SVG visualization module and public snapshot export/import were
also removed. None of these methods is part of the shared core store Protocol.

## Identity, writes and provenance

Stable node IDs normalize target identity. Engagement namespacing prevents
cross-engagement reads/writes; the central store retains the namespace and
original graph mutation identity. Graph writes and their change receipts can
join the same transaction as execution/result settlement. External I/O stays
outside it. Replay of the identical mutation returns its original receipt;
changed content with the same ID fails.

`writes.py`, `predicates.py` and `service.py` validate node types, sensitive
material, property shape and merges. Provenance/evidence references and measured
versus inferred confidence remain. A graph record is not an execution permission.
A verified Finding requires preserved original evidence and the existing origin,
claim review and promotion checks; a successful tool call alone is insufficient.

Retraction records keep the history of withdrawn predicates. Independent predicate
ages prevent an unrelated observation from renewing a stale claim. Actual cleanup
creation/removal records carry original action/evidence IDs; confirmed cleanup
requires the corresponding successful original execution. A new confirmed creation
reopens its obligation. Unmapped resource effects are visible in reports.
Resource effects v2 name a provider-owned backend/tenant identity domain shared
across creation/removal tools. Equal local IDs in different domains are distinct
obligations; legacy v1 effects cannot prove cleanup identity. Removal originals
must also correspond to the current creation, rather than an earlier generation.

Malformed graph/domain records and missing edges/origins are surfaced through
integrity checks rather than replaced by empty successful results. Evidence body
integrity remains the responsibility of the separate original Evidence store.

## Storage format and migration

`snapshot.py` remains an internal versioned record codec used by SQLite and the
explicit legacy migration reader. It serializes detached node/edge models and
checks schema headers, collections, model shape and sensitive properties when
reading. It is not re-exported as a live graph restore/merge API.

`CentralAttackGraphStore` stores graph rows and mutation receipts in the central
file. The separate SQLite implementation is retained for the historical
database format and offline inspection, not an alternative runtime. In-memory stores remain
useful for detached preparation, views and explicit fixtures. They are not a
second operational graph owner in central assembly.

Legacy database import uses the supported plan1c boundary: original source files,
manifest hashes, receipts, original Evidence/Audit/Approval/checkpoints and
accounting must agree. No automatic import or reverse dual-write occurs. User
`data/` files and source DBs are retained. Entered legacy SDK state is not converted
by the current zero-entry preparation helper; unsupported formats fail explicitly.
See [central state](../contracts/central-state.md).

## Compatibility boundary

The explicit legacy runtime still consumes graph pattern/frontier and historical
attempt data, so those implementations were not deleted merely by filename.
They do not control central worker activation, repeated-evidence admission or
termination. Compatibility libraries retain actual readers/consumers until their
public surface is retired or converted; historical reports are not current specs.

Unit tests of retired rendering/traversal APIs were removed. Storage-format fixtures
now use the internal codec explicitly; provenance, merge/retraction, sensitive-value,
integrity, cleanup, original evidence and atomic mutation tests remain. Application
assembly never installs those test helpers.
