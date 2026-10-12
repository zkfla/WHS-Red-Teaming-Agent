"""The single write path into the Shared Attack Graph.

Identity normalization, non-destructive merge and the secret guard are
guaranteed to run here. Docs: ``docs/agent/attack-graph.md``.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, TypeVar

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
    CLAIM_PROVENANCE_KEY,
    CLEANUP_KEY,
    RESERVED_KEYS,
    Provenance,
    apply_retraction,
    build_properties,
    claim_entries_for_fact_keys,
    cleanup_of,
    facts_of,
    validate_properties,
)
from redteam.attack_graph.retraction import PlannedRetraction, RetractionRule, plan_retractions
from redteam.attack_graph.stores.base import require_mutable
from redteam.attack_graph.traversal import Direction
from redteam.attack_graph.view import AttackGraphView
from redteam.attack_graph.writes import (
    NodeTypeConflictError as NodeTypeConflictError,
)
from redteam.attack_graph.writes import (
    UngroundedFindingError as UngroundedFindingError,
)
from redteam.attack_graph.writes import merge_edge, merge_node, update_edge, update_node
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


def _edge_id(
    edge_type: GraphEdgeType, source_id: GraphNodeId, target_id: GraphNodeId
) -> GraphEdgeId:
    """Deterministic, so re-linking the same relation never duplicates it."""
    return GraphEdgeId(f"{edge_type.value}:{source_id}->{target_id}")


def _now() -> datetime:
    return datetime.now(timezone.utc)


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

    async def get_edge(self, edge_id: GraphEdgeId) -> GraphEdge | None:
        return await self._store.get_edge(edge_id)

    async def incident_edges(
        self, node_id: GraphNodeId, *, direction: Direction = "any",
    ) -> list[GraphEdge]:
        return await self._view.incident_edges(node_id, direction=direction)

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
        extra_content_keys: tuple[str, ...] = (),
    ) -> GraphObservation:
        return await self._view.observe(
            step=step,
            max_per_type=max_per_type,
            max_total=max_total,
            max_attempted=max_attempted,
            extra_content_keys=extra_content_keys,
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

        proposal = GraphNode(
            id=node_id, type=node_type, labels=sorted(set(labels or [])),
            properties=incoming, created_at=_now(), updated_at=_now(),
        )
        node = await update_node(
            self._store, node_id, lambda current: merge_node(current, proposal),
        )
        assert node is not None
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
            recorded_claims = raw.get(CLAIM_PROVENANCE_KEY, {})
            if not isinstance(recorded_claims, dict):
                recorded_claims = {}
            fresh_claims = built.get(CLAIM_PROVENANCE_KEY, {})
            if not isinstance(fresh_claims, dict):
                fresh_claims = {}
            # Same per-key rule as the live merge: the fresh write wins where
            # it asserts, recorded history survives everywhere else.
            built[CLAIM_PROVENANCE_KEY] = {**recorded_claims, **fresh_claims}
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

    async def retract(self, node_id: GraphNodeId, key: str, *, step: int, reason: str) -> bool:
        """Withdraw a predicate that no longer holds, with a reason."""
        changed = False
        def withdraw(node: GraphNode | None) -> GraphNode | None:
            nonlocal changed
            if node is None:
                changed = False
                return None
            properties, changed = apply_retraction(node.properties, key, step=step, reason=reason)
            if not changed:
                return node
            return node.model_copy(update={"properties": properties, "updated_at": _now()})
        await update_node(self._store, node_id, withdraw)
        return changed

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
        edge_properties = validate_properties({**(properties or {})})
        if provenance is not None:
            edge_properties["provenance"] = [provenance.model_dump(mode="json")]
            relation_values = {
                key: value
                for key, value in edge_properties.items()
                if key not in ("provenance", CLAIM_PROVENANCE_KEY)
            }
            edge_properties[CLAIM_PROVENANCE_KEY] = claim_entries_for_fact_keys(
                relation_values, provenance, provenance.step,
            )
        proposal = GraphEdge(
            id=edge_id,
            type=edge_type,
            source_id=source_id,
            target_id=target_id,
            properties=edge_properties,
            created_at=_now(),
        )
        edge = await update_edge(
            self._store, edge_id, lambda current: merge_edge(current, proposal),
        )
        assert edge is not None
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
        updated: Hypothesis | None = None
        def resolve(current: GraphNode | None) -> GraphNode | None:
            nonlocal updated
            hypothesis = _read(Hypothesis, current) if current else None
            if hypothesis is None:
                updated = None
                return current
            updated = hypothesis.model_copy(update={
                "status": status,
                "confidence": hypothesis.confidence if confidence is None else confidence,
            })
            incoming = self._normalize_properties(
                None, attrs={"status": status}, facts=updated.model_dump(mode="json"),
                provenance=None, step=step,
            )
            assert current is not None
            return merge_node(current, current.model_copy(update={
                "properties": incoming, "updated_at": _now(),
            }))
        await update_node(self._store, hypothesis_node_id(hypothesis_id), resolve)
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


    async def subgraph_around(
        self, node_id: GraphNodeId, *, radius: int = 1, direction: Direction = "any"
    ) -> tuple[list[GraphNode], list[GraphEdge]]:
        """The neighbourhood of a node, for focused reporting or rendering."""
        return await self._view.subgraph_around(node_id, radius=radius, direction=direction)


    # -- cleanup -----------------------------------------------------

    async def register_created_resource(
        self,
        node_id: GraphNodeId,
        *,
        resource_type: str,
        action_id: str | None = None,
        evidence_id: str | None = None,
        run_id: str | None = None,
        created_at: datetime | None = None,
        step: int = 0,
        note: str = "",
        reopen: bool = False,
    ) -> CleanupItem | None:
        """Record that this run created something it must remove later.

        A resource that was already cleaned is *not* put back to pending;
        pass ``reopen=True`` when it genuinely was created again.
        """
        item: CleanupItem | None = None
        def register(current: GraphNode | None) -> GraphNode | None:
            nonlocal item
            if current is None:
                item = None
                return None
            existing = cleanup_of(current.properties)
            if existing is not None and not reopen:
                item = CleanupItem.model_validate({"node_id": node_id, **existing})
                if not item.is_outstanding:
                    return current
            item = CleanupItem(
                node_id=node_id, resource_type=resource_type, status=CleanupStatus.PENDING,
                created_at_step=step, created_by_action_id=action_id, note=note,
                created_evidence_id=evidence_id, run_id=run_id,
                created_at=created_at,
            )
            return self._with_cleanup(current, item)
        await update_node(self._store, node_id, register)
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
        return await self._update_cleanup(node_id, status=CleanupStatus.NOT_REQUIRED, note=note)

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
        item: CleanupItem | None = None
        def change(current: GraphNode | None) -> GraphNode | None:
            nonlocal item
            existing = cleanup_of(current.properties) if current else None
            if existing is None:
                item = None
                return current
            item = CleanupItem.model_validate({"node_id": node_id, **existing})
            item = item.model_copy(update={
                "status": status, "cleanup_evidence_id": evidence_id or item.cleanup_evidence_id,
                "note": note or item.note,
            })
            assert current is not None
            return self._with_cleanup(current, item)
        await update_node(self._store, node_id, change)
        return item

    @staticmethod
    def _with_cleanup(node: GraphNode, item: CleanupItem) -> GraphNode:
        payload = item.model_dump(mode="json")
        payload.pop("node_id", None)
        return node.model_copy(update={
            "properties": {**node.properties, CLEANUP_KEY: payload}, "updated_at": _now(),
        })

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
        planned = plan_retractions(await self.list_nodes(), current_step=current_step, rules=rules)
        if dry_run:
            return planned
        applied: list[PlannedRetraction] = []
        for item in planned:
            if await self.retract(item.node_id, item.attr, step=current_step, reason=item.reason):
                applied.append(item)
        return applied


    # -- removal -----------------------------------------------------

    async def remove_node(self, node_id: GraphNodeId) -> bool:
        """Drop a node and its edges — for correcting a mistaken write only.
        "No longer true" is :meth:`retract`, which keeps the history."""
        return await require_mutable(self._store, "remove_node").remove_node(node_id)

    async def remove_edge(self, edge_id: GraphEdgeId) -> bool:
        return await require_mutable(self._store, "remove_edge").remove_edge(edge_id)
