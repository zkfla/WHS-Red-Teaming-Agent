from __future__ import annotations

from dataclasses import dataclass, field

from .models import Asset, Evidence, Intent, VulnerabilityCandidate, new_id


@dataclass
class AssetGraph:
    assets: dict[str, Asset] = field(default_factory=dict)

    def upsert(self, asset: Asset) -> Asset:
        key = f"{asset.kind}:{asset.canonical_value.lower()}"
        existing = next((a for a in self.assets.values() if f"{a.kind}:{a.canonical_value.lower()}" == key), None)
        if existing:
            return existing
        self.assets[asset.id] = asset
        return asset


@dataclass(frozen=True)
class ExplorationNode:
    kind: str
    ref_id: str
    id: str = field(default_factory=lambda: new_id("node"))


@dataclass
class ExplorationGraph:
    nodes: dict[str, ExplorationNode] = field(default_factory=dict)
    edges: list[tuple[str, str, str]] = field(default_factory=list)
    anchors: list[tuple[str, str]] = field(default_factory=list)

    def add(self, kind: str, ref_id: str) -> ExplorationNode:
        node = ExplorationNode(kind=kind, ref_id=ref_id)
        self.nodes[node.id] = node
        return node

    def link(self, source: ExplorationNode, relation: str, target: ExplorationNode) -> None:
        self.edges.append((source.id, relation, target.id))

    def anchor(self, node: ExplorationNode, asset: Asset) -> None:
        self.anchors.append((node.id, asset.id))


@dataclass
class EvidenceStore:
    items: dict[str, Evidence] = field(default_factory=dict)

    def add(self, evidence: Evidence) -> Evidence:
        self.items[evidence.id] = evidence
        return evidence

