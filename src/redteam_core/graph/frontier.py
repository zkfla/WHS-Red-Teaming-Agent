from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, replace
from enum import StrEnum

from pydantic import Field

from redteam_core.extension.bundle import ExtensionCatalog
from redteam_core.graph.semantics import GuardEffect, GuardScope, RelationSemantics
from redteam_core.knowledge.state import CoreSession
from redteam_core.models.base import SchemaModel, TriState, stable_id
from redteam_core.models.security import ClaimStatus, Relation

DEFAULT_MAX_DEPTH = 10
DEFAULT_MAX_EXPANSIONS = 5_000
DEFAULT_MAX_FRONTIERS = 5_000


class FrontierConnectivity(StrEnum):
    COMPLETE = "COMPLETE"
    PARTIAL = "PARTIAL"


class FrontierDisposition(StrEnum):
    READY = "READY"
    EXPANDABLE = "EXPANDABLE"
    BLOCKED = "BLOCKED"
    EXHAUSTED = "EXHAUSTED"


class FrontierQuery(SchemaModel):
    source_hints: tuple[str, ...] = ()
    mechanism_hints: tuple[str, ...] = ()
    sink_hints: tuple[str, ...] = ()
    max_depth: int = Field(default=DEFAULT_MAX_DEPTH, ge=1, le=100)
    max_frontiers: int = Field(default=DEFAULT_MAX_FRONTIERS, ge=1, le=100_000)
    max_expansions: int = Field(default=DEFAULT_MAX_EXPANSIONS, ge=0, le=1_000_000)


class AttackFrontier(SchemaModel):
    frontier_id: str = Field(pattern=r"^frontier_[0-9a-f]{16}$")
    connectivity: FrontierConnectivity
    disposition: FrontierDisposition
    claim_status_ceiling: ClaimStatus
    proposal_ready: bool
    source_ref: str = Field(min_length=1, max_length=500)
    current_ref: str = Field(min_length=1, max_length=500)
    sink_ref: str | None = Field(default=None, max_length=500)
    potential_sink_ref: str | None = Field(default=None, max_length=500)
    source_tags: tuple[str, ...] = ()
    mechanism_tags: tuple[str, ...] = ()
    sink_tags: tuple[str, ...] = ()
    relation_ids: tuple[str, ...] = ()
    context_refs: tuple[str, ...] = ()
    guard_relation_ids: tuple[str, ...] = ()
    uncertain_guard_relation_ids: tuple[str, ...] = ()
    missing_reason_ids: tuple[str, ...] = ()
    requirement_ids: tuple[str, ...] = ()
    unknown_relation_ids: tuple[str, ...] = ()
    next_relation_ids: tuple[str, ...] = ()
    influence_proven: bool = False
    objective_score: int = 0


class FrontierSearchResult(SchemaModel):
    frontiers: tuple[AttackFrontier, ...]
    expansions_used: int = Field(ge=0)
    search_complete: bool
    truncated: bool
    depth_limit_reached: bool
    unexplored_source_count: int = Field(ge=0)


@dataclass(frozen=True)
class _PathState:
    nodes: tuple[str, ...]
    relation_ids: tuple[str, ...]
    source_tags: tuple[str, ...]
    mechanism_tags: tuple[str, ...]
    sink_tags: tuple[str, ...]
    context_refs: tuple[str, ...]
    guard_relation_ids: tuple[str, ...]
    uncertain_guard_relation_ids: tuple[str, ...]
    missing_reason_ids: tuple[str, ...]
    requirement_ids: tuple[str, ...]
    unknown_relation_ids: tuple[str, ...]
    influence_proven: bool
    blocked: bool = False


def _semantics(catalog: ExtensionCatalog, relation: Relation) -> RelationSemantics:
    registration = catalog.relation_types.get(relation.relation_type_id)
    return registration.semantics if registration is not None else RelationSemantics()


def _ordered_unique(*groups: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(item for group in groups for item in group))


def _guard_index(
    relations: tuple[Relation, ...], catalog: ExtensionCatalog
) -> dict[str, tuple[Relation, ...]]:
    by_scope: dict[str, list[Relation]] = defaultdict(list)
    for relation in relations:
        semantics = _semantics(catalog, relation)
        if semantics.guard_effect is None:
            continue
        scope_ref = (
            relation.source_ref
            if semantics.guard_scope is GuardScope.SOURCE
            else relation.target_ref
        )
        by_scope[scope_ref].append(relation)
    return {
        key: tuple(sorted(values, key=lambda item: item.relation_id))
        for key, values in by_scope.items()
    }


def _apply_guards(
    path: _PathState,
    node_ref: str,
    guards: dict[str, tuple[Relation, ...]],
    catalog: ExtensionCatalog,
) -> _PathState:
    guard_ids = list(path.guard_relation_ids)
    uncertain_ids = list(path.uncertain_guard_relation_ids)
    missing_ids = list(path.missing_reason_ids)
    requirement_ids = list(path.requirement_ids)
    unknown_ids = list(path.unknown_relation_ids)
    blocked = path.blocked

    for guard in guards.get(node_ref, ()):
        semantics = _semantics(catalog, guard)
        guard_contexts = set(guard.context_refs)
        if (
            guard_contexts
            and path.context_refs
            and not guard_contexts.intersection(path.context_refs)
        ):
            continue
        guard_ids.append(guard.relation_id)
        missing_ids.extend(guard.missing_reason_ids)
        requirement_ids.extend(guard.requirement_ids)
        if guard.verdict is TriState.FALSE:
            continue
        if guard.verdict is TriState.UNKNOWN:
            unknown_ids.append(guard.relation_id)
        if semantics.guard_effect is GuardEffect.BLOCKS:
            if guard.verdict is TriState.TRUE:
                blocked = True
            else:
                uncertain_ids.append(guard.relation_id)
        elif semantics.guard_effect is GuardEffect.MAY_BLOCK:
            uncertain_ids.append(guard.relation_id)

    return replace(
        path,
        guard_relation_ids=_ordered_unique(guard_ids),
        uncertain_guard_relation_ids=_ordered_unique(uncertain_ids),
        missing_reason_ids=_ordered_unique(missing_ids),
        requirement_ids=_ordered_unique(requirement_ids),
        unknown_relation_ids=_ordered_unique(unknown_ids),
        blocked=blocked,
    )


def _extend_context(
    path_contexts: tuple[str, ...], relation: Relation, semantics: RelationSemantics
) -> tuple[str, ...] | None:
    relation_contexts = tuple(sorted(set(relation.context_refs)))
    if not relation_contexts:
        return path_contexts
    if not path_contexts:
        return relation_contexts
    if set(path_contexts) & set(relation_contexts):
        return _ordered_unique(list(path_contexts), list(relation_contexts))
    if semantics.context_bridge:
        return _ordered_unique(list(path_contexts), list(relation_contexts))
    return None


def _frontier_id(path: _PathState) -> str:
    return stable_id(
        "frontier",
        *path.relation_ids,
        *path.context_refs,
        *path.sink_tags,
    )


def _dominance_key(
    path: _PathState,
) -> tuple[str, tuple[str, ...], tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    return (
        path.nodes[-1],
        path.context_refs,
        path.source_tags,
        path.mechanism_tags,
        path.sink_tags,
    )


def _uncertainty_key(path: _PathState) -> frozenset[str]:
    return frozenset((*path.unknown_relation_ids, *path.uncertain_guard_relation_ids))


def _objective_score(path: _PathState, query: FrontierQuery) -> int:
    score = 0
    score += 6 * len(set(path.source_tags) & set(query.source_hints))
    score += 5 * len(set(path.mechanism_tags) & set(query.mechanism_hints))
    score += 7 * len(set(path.sink_tags) & set(query.sink_hints))
    if path.sink_tags:
        score += 3
    if path.influence_proven:
        score += 3
    if path.blocked:
        score -= 4
    score -= len(path.unknown_relation_ids)
    score -= len(path.uncertain_guard_relation_ids)
    return score


def _nearest_sink(
    start_ref: str,
    adjacency: dict[str, list[Relation]],
    catalog: ExtensionCatalog,
    max_depth: int,
) -> str | None:
    queue: deque[tuple[str, int]] = deque([(start_ref, 0)])
    visited = {start_ref}
    while queue:
        node_ref, depth = queue.popleft()
        if depth >= max_depth:
            continue
        for relation in adjacency.get(node_ref, []):
            semantics = _semantics(catalog, relation)
            if semantics.sink_tags:
                return relation.target_ref
            if relation.target_ref not in visited:
                visited.add(relation.target_ref)
                queue.append((relation.target_ref, depth + 1))
    return None


def _to_frontier(
    path: _PathState,
    query: FrontierQuery,
    adjacency: dict[str, list[Relation]],
    catalog: ExtensionCatalog,
    *,
    complete: bool | None = None,
    exhausted: bool = False,
) -> AttackFrontier:
    complete = bool(path.sink_tags) if complete is None else complete
    uncertainty = bool(
        path.unknown_relation_ids
        or path.uncertain_guard_relation_ids
        or path.missing_reason_ids
    )
    if path.blocked:
        disposition = FrontierDisposition.BLOCKED
        ceiling = ClaimStatus.REJECTED
    elif complete:
        disposition = (
            FrontierDisposition.READY if path.influence_proven else FrontierDisposition.EXHAUSTED
        )
        ceiling = (
            ClaimStatus.SUPPORTED
            if path.influence_proven and not uncertainty
            else ClaimStatus.CANDIDATE
        )
    elif exhausted:
        disposition = FrontierDisposition.EXHAUSTED
        ceiling = ClaimStatus.CANDIDATE
    else:
        disposition = FrontierDisposition.EXPANDABLE
        ceiling = ClaimStatus.CANDIDATE

    next_ids = tuple(
        relation.relation_id
        for relation in adjacency.get(path.nodes[-1], [])
        if relation.target_ref not in path.nodes
    )[:8]
    potential_sink = None
    if not complete:
        potential_sink = _nearest_sink(path.nodes[-1], adjacency, catalog, query.max_depth)
    return AttackFrontier(
        frontier_id=_frontier_id(path),
        connectivity=FrontierConnectivity.COMPLETE if complete else FrontierConnectivity.PARTIAL,
        disposition=disposition,
        claim_status_ceiling=ceiling,
        proposal_ready=complete and not path.blocked and path.influence_proven,
        source_ref=path.nodes[0],
        current_ref=path.nodes[-1],
        sink_ref=path.nodes[-1] if complete else None,
        potential_sink_ref=potential_sink,
        source_tags=path.source_tags,
        mechanism_tags=path.mechanism_tags,
        sink_tags=path.sink_tags,
        relation_ids=path.relation_ids,
        context_refs=path.context_refs,
        guard_relation_ids=path.guard_relation_ids,
        uncertain_guard_relation_ids=path.uncertain_guard_relation_ids,
        missing_reason_ids=path.missing_reason_ids,
        requirement_ids=path.requirement_ids,
        unknown_relation_ids=path.unknown_relation_ids,
        next_relation_ids=next_ids,
        influence_proven=path.influence_proven,
        objective_score=_objective_score(path, query),
    )


def _diverse_rank(
    frontiers: list[AttackFrontier], max_frontiers: int
) -> tuple[AttackFrontier, ...]:
    ordered = sorted(
        frontiers,
        key=lambda item: (
            -item.objective_score,
            item.connectivity is not FrontierConnectivity.COMPLETE,
            item.disposition is FrontierDisposition.BLOCKED,
            len(item.relation_ids),
            item.frontier_id,
        ),
    )
    buckets: dict[tuple[str | None, str | None, str | None, str | None], deque[AttackFrontier]] = {}
    for item in ordered:
        key = (
            item.source_tags[0] if item.source_tags else None,
            item.sink_tags[0] if item.sink_tags else None,
            item.mechanism_tags[0] if item.mechanism_tags else None,
            item.context_refs[0] if item.context_refs else None,
        )
        buckets.setdefault(key, deque()).append(item)
    selected: list[AttackFrontier] = []
    while buckets and len(selected) < max_frontiers:
        for key in list(buckets):
            selected.append(buckets[key].popleft())
            if not buckets[key]:
                del buckets[key]
            if len(selected) >= max_frontiers:
                break
    return tuple(selected)


def build_attack_frontiers(
    session: CoreSession,
    *,
    catalog: ExtensionCatalog,
    query: FrontierQuery | None = None,
) -> FrontierSearchResult:
    """Run a deterministic bounded search over registered structural relation semantics."""

    if not catalog.frozen:
        raise RuntimeError("extension catalog must be frozen before Frontier search")
    query = query or FrontierQuery()
    relations = tuple(
        sorted(session.semantic.relations.values(), key=lambda item: item.relation_id)
    )
    traversable = tuple(
        relation for relation in relations if _semantics(catalog, relation).traversable
    )
    adjacency: dict[str, list[Relation]] = defaultdict(list)
    for relation in traversable:
        adjacency[relation.source_ref].append(relation)
    for values in adjacency.values():
        values.sort(key=lambda item: item.relation_id)

    guards = _guard_index(relations, catalog)
    sources = [
        relation for relation in traversable if _semantics(catalog, relation).source_tags
    ]
    queue: deque[_PathState] = deque()
    for relation in sources:
        semantics = _semantics(catalog, relation)
        path = _PathState(
            nodes=(relation.source_ref, relation.target_ref),
            relation_ids=(relation.relation_id,),
            source_tags=semantics.source_tags,
            mechanism_tags=semantics.mechanism_tags,
            sink_tags=semantics.sink_tags,
            context_refs=tuple(sorted(set(relation.context_refs))),
            guard_relation_ids=(),
            uncertain_guard_relation_ids=(),
            missing_reason_ids=tuple(relation.missing_reason_ids),
            requirement_ids=tuple(relation.requirement_ids),
            unknown_relation_ids=(
                (relation.relation_id,) if relation.verdict is TriState.UNKNOWN else ()
            ),
            influence_proven=(
                semantics.proves_influence and relation.verdict is TriState.TRUE
            ),
            blocked=relation.verdict is TriState.FALSE,
        )
        queue.append(_apply_guards(path, relation.target_ref, guards, catalog))

    frontiers: list[AttackFrontier] = []
    expansions = 0
    depth_limit_reached = False
    source_ids = {path.relation_ids[0] for path in queue}
    dominance: dict[
        tuple[str, tuple[str, ...], tuple[str, ...], tuple[str, ...], tuple[str, ...]],
        tuple[int, frozenset[str]],
    ] = {}

    while queue and expansions < query.max_expansions:
        path = queue.popleft()
        current_dominance = dominance.get(_dominance_key(path))
        if current_dominance is not None:
            path_uncertainty = _uncertainty_key(path)
            if (
                current_dominance[0] < len(path.relation_ids)
                or (
                    current_dominance[0] == len(path.relation_ids)
                    and current_dominance[1] < path_uncertainty
                )
            ):
                continue
        last_relation = session.semantic.relations[path.relation_ids[-1]]
        last_semantics = _semantics(catalog, last_relation)
        focused_sink_reached = bool(
            path.sink_tags
            and (not query.sink_hints or set(path.sink_tags) & set(query.sink_hints))
        )

        if path.blocked:
            frontiers.append(_to_frontier(path, query, adjacency, catalog, complete=False))
            continue
        if focused_sink_reached:
            frontiers.append(_to_frontier(path, query, adjacency, catalog, complete=True))
            if not last_semantics.continue_after_sink:
                continue
        elif path.sink_tags and not last_semantics.continue_after_sink:
            frontiers.append(
                _to_frontier(
                    path,
                    query,
                    adjacency,
                    catalog,
                    complete=not query.sink_hints,
                )
            )
            continue
        if len(path.relation_ids) >= query.max_depth:
            depth_limit_reached = True
            frontiers.append(_to_frontier(path, query, adjacency, catalog, exhausted=True))
            continue

        compatible_extensions = 0
        for relation in adjacency.get(path.nodes[-1], []):
            if relation.target_ref in path.nodes:
                continue
            semantics = _semantics(catalog, relation)
            contexts = _extend_context(path.context_refs, relation, semantics)
            if contexts is None:
                continue
            candidate = replace(
                path,
                nodes=(*path.nodes, relation.target_ref),
                relation_ids=(*path.relation_ids, relation.relation_id),
                mechanism_tags=_ordered_unique(
                    list(path.mechanism_tags), list(semantics.mechanism_tags)
                ),
                sink_tags=semantics.sink_tags,
                context_refs=contexts,
                missing_reason_ids=_ordered_unique(
                    list(path.missing_reason_ids), relation.missing_reason_ids
                ),
                requirement_ids=_ordered_unique(
                    list(path.requirement_ids), relation.requirement_ids
                ),
                unknown_relation_ids=_ordered_unique(
                    list(path.unknown_relation_ids),
                    [relation.relation_id] if relation.verdict is TriState.UNKNOWN else [],
                ),
                influence_proven=(
                    path.influence_proven
                    or (semantics.proves_influence and relation.verdict is TriState.TRUE)
                ),
                blocked=path.blocked or relation.verdict is TriState.FALSE,
            )
            candidate = _apply_guards(candidate, relation.target_ref, guards, catalog)
            dominance_key = _dominance_key(candidate)
            uncertainty = _uncertainty_key(candidate)
            previous = dominance.get(dominance_key)
            if (
                previous
                and previous[0] <= len(candidate.relation_ids)
                and previous[1] <= uncertainty
            ):
                continue
            dominance[dominance_key] = (len(candidate.relation_ids), uncertainty)
            queue.append(candidate)
            compatible_extensions += 1
            expansions += 1
            if expansions >= query.max_expansions:
                break
        if compatible_extensions == 0:
            frontiers.append(_to_frontier(path, query, adjacency, catalog, exhausted=True))

    truncated = bool(queue) or len(frontiers) > query.max_frontiers
    selected = _diverse_rank(frontiers, query.max_frontiers)
    queued_sources = {path.relation_ids[0] for path in queue if path.relation_ids}
    return FrontierSearchResult(
        frontiers=selected,
        expansions_used=expansions,
        search_complete=not queue and not depth_limit_reached,
        truncated=truncated,
        depth_limit_reached=depth_limit_reached,
        unexplored_source_count=len(source_ids & queued_sources),
    )
