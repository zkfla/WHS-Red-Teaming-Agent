"""The single write path into the Shared Attack Graph.

Identity normalization, non-destructive merge and the secret guard are
guaranteed to run here. Owned by Agent Team.
Rationale: ``docs/agent/attack-graph.md``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from redteam.attack_graph.identity import (
    finding_node_id,
    hypothesis_node_id,
    make_node_id,
    node_id_for_target,
    node_type_for_target,
)
from redteam.attack_graph.integrity import IntegrityReport
from redteam.attack_graph.observation import (
    ATTEMPTED_FACT_KEY,
    DEFAULT_MAX_PER_TYPE,
    DEFAULT_MAX_TOTAL,
    GraphObservation,
)
from redteam.attack_graph.patterns import NodePattern, ResolveResult
from redteam.attack_graph.predicates import (
    RESERVED_KEYS,
    Provenance,
    apply_retraction,
    build_properties,
    facts_of,
    merge_properties,
)
from redteam.attack_graph.snapshot import from_records, to_records
from redteam.attack_graph.stores.base import require_listable
from redteam.attack_graph.view import AttackGraphView
from redteam.core.contracts.graph import Finding, GraphEdge, GraphNode, Hypothesis
from redteam.core.contracts.target import AnyTarget, target_display_name
from redteam.core.enums import GraphEdgeType, GraphNodeType
from redteam.core.ids import GraphEdgeId, GraphNodeId
from redteam.core.ports.attack_graph import AttackGraphStore

HYPOTHESIS_OPEN = "open"
HYPOTHESIS_CONFIRMED = "confirmed"
HYPOTHESIS_REJECTED = "rejected"
HYPOTHESIS_INCONCLUSIVE = "inconclusive"


class UngroundedFindingError(ValueError):
    """Raised when a verified Finding is recorded without Evidence."""


def _edge_id(
    edge_type: GraphEdgeType, source_id: GraphNodeId, target_id: GraphNodeId
) -> GraphEdgeId:
    """Deterministic, so re-linking the same relation never duplicates it."""
    return GraphEdgeId(f"{edge_type.value}:{source_id}->{target_id}")


def _now() -> datetime:
    return datetime.now(timezone.utc)


class AttackGraphService:
    """Write path over an ``AttackGraphStore``.

    Node/edge operations need only the frozen ``core`` port; enumeration
    additionally needs ``stores.base.ListableAttackGraphStore``.
    """

    def __init__(self, store: AttackGraphStore) -> None:
        self._store = store
        self._view = AttackGraphView(store)

    # -- reads -------------------------------------------------------

    def view(self) -> AttackGraphView:
        """A read-only handle to give components that must not write."""
        return self._view

    async def get_node(self, node_id: GraphNodeId) -> GraphNode | None:
        return await self._store.get_node(node_id)

    async def neighbors(self, node_id: GraphNodeId) -> list[GraphNode]:
        return await self._store.neighbors(node_id)

    async def query(self, **filters: Any) -> list[GraphNode]:
        return await self._store.query(**filters)

    async def list_nodes(self) -> list[GraphNode]:
        return await self._view.list_nodes()

    async def list_edges(self) -> list[GraphEdge]:
        return await self._view.list_edges()

    async def observe(
        self,
        *,
        step: int = 0,
        max_per_type: int = DEFAULT_MAX_PER_TYPE,
        max_total: int = DEFAULT_MAX_TOTAL,
    ) -> GraphObservation:
        return await self._view.observe(
            step=step, max_per_type=max_per_type, max_total=max_total
        )

    async def resolve(
        self,
        patterns: list[NodePattern],
        *,
        current_step: int = 0,
        params: dict[str, Any] | None = None,
        for_frontier: bool = False,
    ) -> ResolveResult:
        return await self._view.resolve(
            patterns,
            current_step=current_step,
            params=params,
            for_frontier=for_frontier,
        )

    async def integrity_report(self) -> IntegrityReport:
        return await self._view.integrity_report()

    # -- node writes -------------------------------------------------

    async def upsert_node(
        self,
        node_type: GraphNodeType,
        properties: dict[str, Any] | None = None,
        node_id: GraphNodeId | None = None,
        *,
        key: str | None = None,
        labels: list[str] | None = None,
        attrs: dict[str, Any] | None = None,
        facts: dict[str, Any] | None = None,
        provenance: Provenance | None = None,
        step: int = 0,
    ) -> GraphNode:
        """Add or merge a node. Never destructive.

        ``properties`` takes either the structured layout or a plain dict
        of observed values, treated as ``facts``.
        """
        if node_id is None:
            if key is None:
                raise ValueError("upsert_node needs either node_id or key")
            node_id = make_node_id(node_type, key)

        incoming = self._normalize_properties(
            properties, attrs=attrs, facts=facts, provenance=provenance, step=step
        )

        existing = await self._store.get_node(node_id)
        if existing is None:
            node = GraphNode(
                id=node_id,
                type=node_type,
                labels=sorted(set(labels or [])),
                properties=incoming,
                created_at=_now(),
                updated_at=_now(),
            )
        else:
            node = existing.model_copy(
                update={
                    "labels": sorted(set(existing.labels) | set(labels or [])),
                    "properties": merge_properties(existing.properties, incoming),
                    "updated_at": _now(),
                }
            )
        await self._store.add_node(node)
        return node

    @staticmethod
    def _normalize_properties(
        properties: dict[str, Any] | None,
        *,
        attrs: dict[str, Any] | None,
        facts: dict[str, Any] | None,
        provenance: Provenance | None,
        step: int,
    ) -> dict[str, Any]:
        raw = dict(properties or {})
        structured = bool(raw) and bool(RESERVED_KEYS & raw.keys())

        merged_attrs = dict(attrs or {})
        merged_facts = dict(facts or {})
        extra: dict[str, Any] = {}

        if structured:
            merged_attrs = {**raw.get("attrs", {}), **merged_attrs}
            merged_facts = {**raw.get("facts", {}), **merged_facts}
            extra = {key: value for key, value in raw.items() if key not in RESERVED_KEYS}
        elif raw:
            merged_facts = {**raw, **merged_facts}

        built = build_properties(
            attrs=merged_attrs,
            facts=merged_facts,
            provenance=provenance,
            step=step,
            extra=extra,
        )
        if structured:
            # Preserve an already-recorded history rather than resetting it.
            built["provenance"] = [
                *raw.get("provenance", []),
                *built["provenance"],
            ]
            built["retractions"] = [*raw.get("retractions", []), *built["retractions"]]
            built["first_seen_step"] = min(raw.get("first_seen_step", step), step)
            built["last_seen_step"] = max(raw.get("last_seen_step", step), step)
        return built

    async def observe_target(
        self,
        target: AnyTarget,
        *,
        attrs: dict[str, Any] | None = None,
        facts: dict[str, Any] | None = None,
        labels: list[str] | None = None,
        provenance: Provenance | None = None,
        step: int = 0,
    ) -> GraphNode:
        """Record an ``ActionProposal.target`` as a node, id derived consistently."""
        return await self.upsert_node(
            node_type_for_target(target),
            node_id=node_id_for_target(target),
            attrs=attrs,
            facts={"display_name": target_display_name(target), **(facts or {})},
            labels=labels,
            provenance=provenance,
            step=step,
        )

    async def retract(
        self, node_id: GraphNodeId, key: str, *, step: int, reason: str
    ) -> bool:
        """Withdraw a predicate that no longer holds, with a reason."""
        node = await self._store.get_node(node_id)
        if node is None:
            return False
        properties, changed = apply_retraction(node.properties, key, step=step, reason=reason)
        if not changed:
            return False
        await self._store.add_node(
            node.model_copy(update={"properties": properties, "updated_at": _now()})
        )
        return True

    async def record_attempt(
        self,
        node_id: GraphNodeId,
        operation_key: str,
        *,
        step: int = 0,
        provenance: Provenance | None = None,
    ) -> GraphNode | None:
        """Note that ``operation_key`` was tried against this node.

        Surfaced by ``build_observation`` as ``attempted``.
        """
        node = await self._store.get_node(node_id)
        if node is None:
            return None
        attempted = list(facts_of(node.properties).get(ATTEMPTED_FACT_KEY, []) or [])
        if operation_key not in attempted:
            attempted.append(operation_key)
        return await self.upsert_node(
            node.type,
            node_id=node_id,
            facts={ATTEMPTED_FACT_KEY: attempted},
            provenance=provenance,
            step=step,
        )

    # -- edges -------------------------------------------------------

    async def link(
        self,
        edge_type: GraphEdgeType,
        source_id: GraphNodeId,
        target_id: GraphNodeId,
        properties: dict[str, Any] | None = None,
        *,
        provenance: Provenance | None = None,
    ) -> GraphEdge:
        """Connect two nodes. The same relation twice stays one edge."""
        edge_id = _edge_id(edge_type, source_id, target_id)
        existing = await self._store.get_edge(edge_id)
        edge_properties = {**(properties or {})}
        if provenance is not None:
            edge_properties["provenance"] = [
                *(existing.properties.get("provenance", []) if existing else []),
                provenance.model_dump(mode="json"),
            ]

        edge = GraphEdge(
            id=edge_id,
            type=edge_type,
            source_id=source_id,
            target_id=target_id,
            properties={**(existing.properties if existing else {}), **edge_properties},
            created_at=existing.created_at if existing else _now(),
        )
        await self._store.add_edge(edge)
        return edge

    # -- hypotheses --------------------------------------------------

    async def record_hypothesis(
        self,
        hypothesis: Hypothesis,
        *,
        about: GraphNodeId | None = None,
        step: int = 0,
        provenance: Provenance | None = None,
    ) -> GraphNode:
        node = await self.upsert_node(
            GraphNodeType.HYPOTHESIS,
            node_id=hypothesis_node_id(hypothesis.hypothesis_id),
            facts=hypothesis.model_dump(mode="json"),
            attrs={"status": hypothesis.status},
            provenance=provenance,
            step=step,
        )
        if about is not None:
            await self.link(GraphEdgeType.TARGETS, node.id, about, provenance=provenance)
        return node

    async def get_hypothesis(self, hypothesis_id: str) -> Hypothesis | None:
        node = await self._store.get_node(hypothesis_node_id(hypothesis_id))
        if node is None:
            return None
        return Hypothesis.model_validate(facts_of(node.properties))

    async def resolve_hypothesis(
        self,
        hypothesis_id: str,
        *,
        status: str,
        confidence: float | None = None,
        step: int = 0,
    ) -> Hypothesis | None:
        """Close out a hypothesis as confirmed/rejected/inconclusive."""
        hypothesis = await self.get_hypothesis(hypothesis_id)
        if hypothesis is None:
            return None
        updated = hypothesis.model_copy(
            update={
                "status": status,
                "confidence": hypothesis.confidence if confidence is None else confidence,
            }
        )
        await self.upsert_node(
            GraphNodeType.HYPOTHESIS,
            node_id=hypothesis_node_id(hypothesis_id),
            facts=updated.model_dump(mode="json"),
            attrs={"status": status},
            step=step,
        )
        return updated

    async def list_hypotheses(self, *, status: str | None = None) -> list[Hypothesis]:
        nodes = await self._store.query(node_type=GraphNodeType.HYPOTHESIS)
        hypotheses = [Hypothesis.model_validate(facts_of(node.properties)) for node in nodes]
        if status is not None:
            hypotheses = [item for item in hypotheses if item.status == status]
        return sorted(hypotheses, key=lambda item: item.hypothesis_id)

    # -- findings ----------------------------------------------------

    async def record_finding(
        self,
        finding: Finding,
        *,
        hypothesis_id: str | None = None,
        about: GraphNodeId | None = None,
        step: int = 0,
        provenance: Provenance | None = None,
    ) -> GraphNode:
        if finding.verified and not finding.evidence_ids:
            raise UngroundedFindingError(
                f"finding {finding.finding_id!r} is marked verified but carries no "
                f"evidence_ids — a claim nobody can check must not enter the graph"
            )

        node = await self.upsert_node(
            GraphNodeType.FINDING,
            node_id=finding_node_id(finding.finding_id),
            facts=finding.model_dump(mode="json"),
            attrs={"verified": finding.verified, "severity": finding.severity},
            provenance=provenance,
            step=step,
        )
        if hypothesis_id is not None:
            await self.link(
                GraphEdgeType.SUPPORTED_BY,
                node.id,
                hypothesis_node_id(hypothesis_id),
                provenance=provenance,
            )
        if about is not None:
            await self.link(GraphEdgeType.TARGETS, node.id, about, provenance=provenance)
        return node

    async def get_finding(self, finding_id: str) -> Finding | None:
        node = await self._store.get_node(finding_node_id(finding_id))
        if node is None:
            return None
        return Finding.model_validate(facts_of(node.properties))

    async def list_findings(self, *, verified: bool | None = None) -> list[Finding]:
        nodes = await self._store.query(node_type=GraphNodeType.FINDING)
        findings = [Finding.model_validate(facts_of(node.properties)) for node in nodes]
        if verified is not None:
            findings = [item for item in findings if item.verified is verified]
        return sorted(findings, key=lambda item: item.finding_id)

    # -- snapshots ---------------------------------------------------

    async def export_snapshot(
        self, *, metadata: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        listable = require_listable(self._store, "export_snapshot")
        return to_records(
            await listable.list_nodes(), await listable.list_edges(), metadata=metadata
        )

    async def import_snapshot(self, records: list[dict[str, Any]]) -> dict[str, Any]:
        nodes, edges, metadata = from_records(records)
        for node in nodes:
            await self._store.add_node(node)
        for edge in edges:
            await self._store.add_edge(edge)
        return metadata
