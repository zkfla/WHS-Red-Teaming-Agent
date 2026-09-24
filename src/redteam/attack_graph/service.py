"""The single write path into the Shared Attack Graph.

Identity normalization, non-destructive merge and the secret guard are
guaranteed to run here. Docs: ``docs/agent/attack-graph.md``.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypeVar
from weakref import WeakKeyDictionary

from redteam.attack_graph.cleanup import CleanupItem, CleanupReport, CleanupStatus, collect
from redteam.attack_graph.identity import (
    OBSERVED_TARGET_FACT,
    finding_node_id,
    hypothesis_node_id,
    make_node_id,
    node_id_for_target,
    node_type_for_target,
)
from redteam.attack_graph.integrity import IntegrityReport
from redteam.attack_graph.observation import (
    ATTEMPTED_FACT_KEY,
    DEFAULT_MAX_ATTEMPTED,
    DEFAULT_MAX_PER_TYPE,
    DEFAULT_MAX_TOTAL,
    GraphObservation,
)
from redteam.attack_graph.patterns import NodePattern, ResolveResult
from redteam.attack_graph.predicates import (
    CLEANUP_KEY,
    MAX_PROVENANCE_ENTRIES,
    RESERVED_KEYS,
    Provenance,
    apply_retraction,
    build_properties,
    cleanup_of,
    facts_of,
    merge_properties,
    validate_properties,
)
from redteam.attack_graph.retraction import PlannedRetraction, RetractionRule, plan_retractions
from redteam.attack_graph.snapshot import from_records, to_records
from redteam.attack_graph.stores.base import require_listable, require_mutable
from redteam.attack_graph.traversal import (
    Direction,
    EvidenceChain,
    ancestors,
    descendants,
    evidence_chain,
    shortest_path,
    subgraph_around,
)
from redteam.attack_graph.view import AttackGraphView
from redteam.attack_graph.visualize import write_html
from redteam.core.contracts.graph import Finding, GraphEdge, GraphNode, Hypothesis
from redteam.core.contracts.target import AnyTarget, target_display_name
from redteam.core.enums import GraphEdgeType, GraphNodeType
from redteam.core.ids import GraphEdgeId, GraphNodeId
from redteam.core.ports.attack_graph import AttackGraphStore

logger = logging.getLogger(__name__)

HYPOTHESIS_OPEN = "open"
HYPOTHESIS_CONFIRMED = "confirmed"
HYPOTHESIS_REJECTED = "rejected"
HYPOTHESIS_INCONCLUSIVE = "inconclusive"


class UngroundedFindingError(ValueError):
    """Raised when a verified Finding is recorded without Evidence."""


_Record = TypeVar("_Record", Hypothesis, Finding)


def _read(model: type[_Record], node: GraphNode) -> _Record | None:
    """Parse a Hypothesis/Finding off a node, or ``None`` if it will not.

    One unreadable node must not take a whole listing down with it.
    ``check_integrity`` reports them as blocking, so they are skipped but
    never hidden.
    """
    try:
        return model.model_validate(facts_of(node.properties))
    except Exception as exc:  # noqa: BLE001 - any parse failure is the same problem
        logger.warning("skipping unreadable %s node %s: %s", model.__name__, node.id, exc)
        return None


class NodeTypeConflictError(ValueError):
    """Raised when an upsert would change an existing node's type. Silently
    keeping the old one lets a caller believe a write landed when it did not."""


def _edge_id(
    edge_type: GraphEdgeType, source_id: GraphNodeId, target_id: GraphNodeId
) -> GraphEdgeId:
    """Deterministic, so re-linking the same relation never duplicates it."""
    return GraphEdgeId(f"{edge_type.value}:{source_id}->{target_id}")


def _now() -> datetime:
    return datetime.now(timezone.utc)


#: Every write is read-modify-write, so concurrent writers to one node lose
#: each other's changes once the store actually awaits I/O. Striped so the
#: table cannot grow with the graph; keyed on the *store* so the service
#: instances sharing one still serialise.
_LOCK_STRIPES = 64
_STORE_LOCKS: WeakKeyDictionary[Any, list[asyncio.Lock]] = WeakKeyDictionary()


def _node_lock(store: Any, node_id: GraphNodeId) -> asyncio.Lock:
    try:
        stripes = _STORE_LOCKS[store]
    except (KeyError, TypeError):
        stripes = [asyncio.Lock() for _ in range(_LOCK_STRIPES)]
        try:
            _STORE_LOCKS[store] = stripes
        except TypeError:  # store is not weak-referenceable
            return stripes[hash(str(node_id)) % _LOCK_STRIPES]
    return stripes[hash(str(node_id)) % _LOCK_STRIPES]


class AttackGraphService:
    """Write path over an ``AttackGraphStore``. Node/edge operations need
    only the frozen ``core`` port; enumeration needs ``ListableAttackGraphStore``."""

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
        max_attempted: int = DEFAULT_MAX_ATTEMPTED,
    ) -> GraphObservation:
        return await self._view.observe(
            step=step,
            max_per_type=max_per_type,
            max_total=max_total,
            max_attempted=max_attempted,
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

    async def has_attempted(self, node_id: GraphNodeId, operation_key: str) -> bool:
        """Was ``operation_key`` already tried against this node? Authoritative."""
        return await self._view.has_attempted(node_id, operation_key)

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
        step: int | None = 0,
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

        async with _node_lock(self._store, node_id):
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
                if existing.type is not node_type:
                    raise NodeTypeConflictError(
                        f"{node_id} is a {existing.type.value} node; refusing to upsert "
                        f"it as {node_type.value}"
                    )
                merged_labels = sorted(set(existing.labels) | set(labels or []))
                merged_properties = merge_properties(existing.properties, incoming)
                # A write that changes nothing is not a write.
                if (
                    merged_labels == existing.labels
                    and merged_properties == existing.properties
                ):
                    return existing
                node = existing.model_copy(
                    update={
                        "labels": merged_labels,
                        "properties": merged_properties,
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
        step: int | None,
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
        # `extra` covers custom keys the caller passed explicitly; a
        # structured properties dict can carry its own, so scan the result.
        validate_properties(built)
        if structured:
            # Preserve an already-recorded history rather than resetting it.
            built["provenance"] = [
                *raw.get("provenance", []),
                *built["provenance"],
            ]
            built["retractions"] = [*raw.get("retractions", []), *built["retractions"]]
            recorded_first = raw.get("first_seen_step", 0)
            recorded_last = raw.get("last_seen_step", 0)
            if step is None:
                # No step supplied (a snapshot re-import): the recorded
                # history stands. Defaulting to 0 would rewrite it.
                built["first_seen_step"] = recorded_first
                built["last_seen_step"] = recorded_last
            else:
                built["first_seen_step"] = min(recorded_first, step)
                built["last_seen_step"] = max(recorded_last, step)
            if CLEANUP_KEY in raw and CLEANUP_KEY not in built:
                built[CLEANUP_KEY] = raw[CLEANUP_KEY]
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
        observed: dict[str, Any] = {}
        literal = getattr(target, "url", None) or getattr(target, "path", None)
        if literal:
            # The id normalises identifier segments; keep the address that
            # was actually reached so a later action can use it.
            observed[OBSERVED_TARGET_FACT] = literal
        return await self.upsert_node(
            node_type_for_target(target),
            node_id=node_id_for_target(target),
            attrs=attrs,
            facts={
                "display_name": target_display_name(target),
                **observed,
                **(facts or {}),
            },
            labels=labels,
            provenance=provenance,
            step=step,
        )

    async def retract(
        self, node_id: GraphNodeId, key: str, *, step: int, reason: str
    ) -> bool:
        """Withdraw a predicate that no longer holds, with a reason."""
        async with _node_lock(self._store, node_id):
            node = await self._store.get_node(node_id)
            if node is None:
                return False
            properties, changed = apply_retraction(
                node.properties, key, step=step, reason=reason
            )
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
        attempted = facts_of(node.properties).get(ATTEMPTED_FACT_KEY) or []
        if operation_key in attempted:
            return node  # nothing to write; the merge is the expensive part
        return await self.upsert_node(
            node.type,
            node_id=node_id,
            facts={ATTEMPTED_FACT_KEY: [operation_key]},
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
        edge_properties = validate_properties({**(properties or {})})
        if provenance is not None:
            # Same ceiling as nodes: re-linking every turn would otherwise
            # grow one edge without bound.
            recorded = [
                *(existing.properties.get("provenance", []) if existing else []),
                provenance.model_dump(mode="json"),
            ]
            edge_properties["provenance"] = (
                recorded
                if len(recorded) <= MAX_PROVENANCE_ENTRIES
                else [recorded[0], *recorded[-(MAX_PROVENANCE_ENTRIES - 1) :]]
            )

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
        return None if node is None else _read(Hypothesis, node)

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
        hypotheses = [h for h in (_read(Hypothesis, node) for node in nodes) if h is not None]
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
        return None if node is None else _read(Finding, node)

    async def list_findings(self, *, verified: bool | None = None) -> list[Finding]:
        nodes = await self._store.query(node_type=GraphNodeType.FINDING)
        findings = [f for f in (_read(Finding, node) for node in nodes) if f is not None]
        if verified is not None:
            findings = [item for item in findings if item.verified is verified]
        return sorted(findings, key=lambda item: item.finding_id)

    # -- traversal ---------------------------------------------------

    async def shortest_path(
        self,
        source_id: GraphNodeId,
        target_id: GraphNodeId,
        *,
        direction: Direction = "out",
        edge_types: set[GraphEdgeType] | None = None,
    ) -> list[GraphEdge] | None:
        """The edges linking two nodes, or ``None`` if they are unlinked."""
        return shortest_path(
            source_id,
            target_id,
            edges=await self.list_edges(),
            direction=direction,
            edge_types=edge_types,
        )

    async def descendants(
        self, node_id: GraphNodeId, *, max_depth: int | None = None
    ) -> list[GraphNode]:
        """Everything reachable by following edges out of ``node_id``."""
        reached = descendants(node_id, edges=await self.list_edges(), max_depth=max_depth)
        return [node for node in await self.list_nodes() if node.id in reached]

    async def ancestors(
        self, node_id: GraphNodeId, *, max_depth: int | None = None
    ) -> list[GraphNode]:
        """Everything that leads to ``node_id``."""
        reached = ancestors(node_id, edges=await self.list_edges(), max_depth=max_depth)
        return [node for node in await self.list_nodes() if node.id in reached]

    async def subgraph_around(
        self, node_id: GraphNodeId, *, radius: int = 1, direction: Direction = "any"
    ) -> tuple[list[GraphNode], list[GraphEdge]]:
        """The neighbourhood of a node, for focused reporting or rendering."""
        return subgraph_around(
            node_id,
            nodes=await self.list_nodes(),
            edges=await self.list_edges(),
            radius=radius,
            direction=direction,
        )

    async def evidence_chain(self, finding_id: str) -> EvidenceChain | None:
        """How a Finding came to be — hypothesis, target, evidence ids."""
        return evidence_chain(
            finding_node_id(finding_id),
            nodes=await self.list_nodes(),
            edges=await self.list_edges(),
        )

    async def evidence_chains(self) -> list[EvidenceChain]:
        """Every Finding's chain, worst severity first."""
        nodes = await self.list_nodes()
        edges = await self.list_edges()
        chains = [
            chain
            for chain in (
                evidence_chain(node.id, nodes=nodes, edges=edges)
                for node in nodes
                if node.type is GraphNodeType.FINDING
            )
            if chain is not None
        ]
        rank = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}
        return sorted(chains, key=lambda item: (rank.get(item.severity, 9), item.finding_id))

    # -- cleanup -----------------------------------------------------

    async def register_created_resource(
        self,
        node_id: GraphNodeId,
        *,
        resource_type: str,
        action_id: str | None = None,
        step: int = 0,
        note: str = "",
        reopen: bool = False,
    ) -> CleanupItem | None:
        """Record that this run created something it must remove later.

        A resource that was already cleaned is *not* put back to pending;
        pass ``reopen=True`` when it genuinely was created again.
        """
        node = await self._store.get_node(node_id)
        if node is None:
            return None

        existing = cleanup_of(node.properties)
        if existing is not None and not reopen:
            current = CleanupItem.model_validate({"node_id": node_id, **existing})
            if not current.is_outstanding:
                return current

        item = CleanupItem(
            node_id=node_id,
            resource_type=resource_type,
            status=CleanupStatus.PENDING,
            created_at_step=step,
            created_by_action_id=action_id,
            note=note,
        )
        await self._write_cleanup(node, item)
        return item

    async def mark_cleaned(
        self, node_id: GraphNodeId, *, evidence_id: str, note: str = ""
    ) -> CleanupItem | None:
        """Close out a resource. Evidence is required — an unbacked claim of
        removal is the same problem as an ungrounded finding."""
        if not evidence_id:
            raise ValueError("marking a resource cleaned needs evidence that it happened")
        return await self._update_cleanup(
            node_id, status=CleanupStatus.CLEANED, evidence_id=evidence_id, note=note
        )

    async def mark_cleanup_failed(self, node_id: GraphNodeId, *, note: str) -> CleanupItem | None:
        return await self._update_cleanup(node_id, status=CleanupStatus.FAILED, note=note)

    async def mark_cleanup_not_required(
        self, node_id: GraphNodeId, *, note: str
    ) -> CleanupItem | None:
        return await self._update_cleanup(
            node_id, status=CleanupStatus.NOT_REQUIRED, note=note
        )

    async def cleanup_report(self) -> CleanupReport:
        return collect(await self.list_nodes())

    async def pending_cleanup(self) -> list[CleanupItem]:
        return (await self.cleanup_report()).outstanding

    async def _update_cleanup(
        self,
        node_id: GraphNodeId,
        *,
        status: CleanupStatus,
        evidence_id: str | None = None,
        note: str = "",
    ) -> CleanupItem | None:
        node = await self._store.get_node(node_id)
        if node is None:
            return None
        existing = cleanup_of(node.properties)
        if existing is None:
            return None
        item = CleanupItem.model_validate({"node_id": node_id, **existing})
        item = item.model_copy(
            update={
                "status": status,
                "cleanup_evidence_id": evidence_id or item.cleanup_evidence_id,
                "note": note or item.note,
            }
        )
        await self._write_cleanup(node, item)
        return item

    async def _write_cleanup(self, node: GraphNode, item: CleanupItem) -> None:
        payload = item.model_dump(mode="json")
        payload.pop("node_id", None)
        async with _node_lock(self._store, node.id):
            # Re-read inside the lock: the node may have moved on.
            current = await self._store.get_node(node.id) or node
            await self._store.add_node(
                current.model_copy(
                    update={
                        "properties": {**current.properties, CLEANUP_KEY: payload},
                        "updated_at": _now(),
                    }
                )
            )

    # -- retraction sweep --------------------------------------------

    async def sweep_stale_predicates(
        self,
        *,
        current_step: int,
        rules: list[RetractionRule] | None = None,
        dry_run: bool = False,
    ) -> list[PlannedRetraction]:
        """Withdraw predicates whose observations went stale. Call once per
        turn; ``dry_run`` reports without writing."""
        planned = plan_retractions(
            await self.list_nodes(), current_step=current_step, rules=rules
        )
        if dry_run:
            return planned
        applied: list[PlannedRetraction] = []
        for item in planned:
            if await self.retract(
                item.node_id, item.attr, step=current_step, reason=item.reason
            ):
                applied.append(item)
        return applied

    # -- rendering ---------------------------------------------------

    async def export_html(
        self,
        path: str | Path,
        *,
        title: str = "Attack Graph",
        around: GraphNodeId | None = None,
        radius: int = 2,
    ) -> Path:
        """Write a self-contained HTML view. ``around`` renders one node's
        neighbourhood, which is what a per-finding report wants."""
        if around is None:
            nodes, edges = await self.list_nodes(), await self.list_edges()
        else:
            nodes, edges = await self.subgraph_around(around, radius=radius)
        return write_html(nodes, edges, path, title=title)

    # -- snapshots ---------------------------------------------------

    async def export_snapshot(
        self, *, metadata: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        listable = require_listable(self._store, "export_snapshot")
        return to_records(
            await listable.list_nodes(), await listable.list_edges(), metadata=metadata
        )

    async def import_snapshot(
        self, records: list[dict[str, Any]], *, merge: bool = False
    ) -> dict[str, Any]:
        """Load a snapshot back into the store.

        Replaces same-id nodes by default (a restore should reproduce the
        snapshot exactly); ``merge=True`` folds it into what is already
        there, for combining two runs.
        """
        nodes, edges, metadata = from_records(records)
        # A snapshot is an external file. Importing without the guard makes
        # "write it to a file, read it back" the way around every check.
        for node in nodes:
            validate_properties(node.properties)
        for edge in edges:
            validate_properties(edge.properties)
        for node in nodes:
            if merge:
                await self.upsert_node(
                    node.type,
                    node.properties,
                    node_id=node.id,
                    labels=list(node.labels),
                    step=None,
                )
            else:
                await self._store.add_node(node)
        for edge in edges:
            await self._store.add_edge(edge)
        return metadata

    # -- removal -----------------------------------------------------

    async def remove_node(self, node_id: GraphNodeId) -> bool:
        """Drop a node and its edges — for correcting a mistaken write only.
        "No longer true" is :meth:`retract`, which keeps the history."""
        return await require_mutable(self._store, "remove_node").remove_node(node_id)

    async def remove_edge(self, edge_id: GraphEdgeId) -> bool:
        return await require_mutable(self._store, "remove_edge").remove_edge(edge_id)
