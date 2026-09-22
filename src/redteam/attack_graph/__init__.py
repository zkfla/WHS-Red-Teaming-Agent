"""Shared Attack Graph — the Source of Truth for security domain state.

Owned by Agent Team. Runtime execution state belongs in
``orchestration.state.RuntimeState``, which references graph nodes by id.

```text
identity     stable "<type>:<key>" node ids
predicates   GraphNode.properties layout, merge rules, secret guard
patterns     preconditions / frontier
traversal    reachability, paths, evidence chains
retraction   which predicates went stale
cleanup      resources this run must remove
observation  bounded summary for a planner
integrity    is this graph safe to report from
snapshot     append-only JSONL export/import
visualize    self-contained HTML/SVG render
view         read-only handle
service      the single write path
stores/      AttackGraphStore implementations
```

Docs: ``docs/agent/attack-graph.md``.
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
from redteam.attack_graph.service import (
    AttackGraphService,
    NodeTypeConflictError,
    UngroundedFindingError,
)
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
    "NodeTypeConflictError",
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
