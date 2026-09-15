"""Shared Attack Graph. Owned by Agent Team.

The Source of Truth for security domain state: Host, Service, Endpoint,
Principal, Credential, Asset, Resource, Hypothesis, Finding — and the
relations between them. Runtime execution state (loop counters, node
names, retry counts) belongs in ``orchestration.state.RuntimeState``,
which references graph nodes by id and never copies them.

Layout:

```text
identity.py     stable "<type>:<key>" node ids + path normalization
predicates.py   how state is written into GraphNode.properties (+ secret guard)
patterns.py     preconditions: "which nodes could this action run against?"
observation.py  bounded summary handed to a planner
integrity.py    "is this graph safe to report from?"
snapshot.py     append-only JSONL export/import for replay
view.py         read-only handle for components that must not write
service.py      the single write path
stores/         AttackGraphStore implementations (in-memory today)
```

Docs: ``docs/agent/attack-graph.md``. Contract: ``redteam.core.contracts.graph``.
"""

from redteam.attack_graph.cleanup import CleanupItem, CleanupReport, CleanupStatus
from redteam.attack_graph.identity import (
    endpoint_node_id,
    host_node_id,
    make_node_id,
    node_id_for_target,
    node_type_for_target,
    normalize_path,
    split_node_id,
)
from redteam.attack_graph.integrity import IntegrityReport, check_integrity
from redteam.attack_graph.observation import GraphObservation, build_observation
from redteam.attack_graph.patterns import NodePattern, ResolveResult, resolve
from redteam.attack_graph.predicates import (
    Confidence,
    Provenance,
    SensitiveValueError,
    attrs_of,
    build_properties,
    facts_of,
    merge_properties,
)
from redteam.attack_graph.retraction import (
    DEFAULT_RETRACTION_RULES,
    PlannedRetraction,
    RetractionRule,
    plan_retractions,
)
from redteam.attack_graph.service import AttackGraphService, UngroundedFindingError
from redteam.attack_graph.snapshot import SnapshotError, from_records, to_records
from redteam.attack_graph.stores.memory import InMemoryAttackGraphStore
from redteam.attack_graph.traversal import (
    ChainLink,
    EvidenceChain,
    ancestors,
    descendants,
    evidence_chain,
    shortest_path,
    subgraph_around,
)
from redteam.attack_graph.view import AttackGraphView
from redteam.attack_graph.visualize import render_html, write_html

__all__ = [
    "AttackGraphService",
    "AttackGraphView",
    "InMemoryAttackGraphStore",
    "GraphObservation",
    "build_observation",
    "NodePattern",
    "ResolveResult",
    "resolve",
    "IntegrityReport",
    "check_integrity",
    "CleanupItem",
    "CleanupReport",
    "CleanupStatus",
    "RetractionRule",
    "PlannedRetraction",
    "plan_retractions",
    "DEFAULT_RETRACTION_RULES",
    "EvidenceChain",
    "ChainLink",
    "evidence_chain",
    "shortest_path",
    "descendants",
    "ancestors",
    "subgraph_around",
    "render_html",
    "write_html",
    "Provenance",
    "Confidence",
    "SensitiveValueError",
    "UngroundedFindingError",
    "SnapshotError",
    "to_records",
    "from_records",
    "attrs_of",
    "facts_of",
    "build_properties",
    "merge_properties",
    "make_node_id",
    "split_node_id",
    "host_node_id",
    "endpoint_node_id",
    "node_id_for_target",
    "node_type_for_target",
    "normalize_path",
]
