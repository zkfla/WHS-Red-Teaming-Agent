from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Literal

from pydantic import Field

from redteam_core.diagnostics.performance import PerformanceStage, performance_span
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
    budget_version: Literal["legacy", "v2"] = "legacy"
    max_work_units: int = Field(default=100_000, ge=0, le=10_000_000)
    max_queue_states: int = Field(default=5_000, ge=0, le=100_000)
    max_aux_visits: int = Field(default=10_000, ge=0, le=1_000_000)
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
    sink_memo_capacity_reached: bool = False
    budget_version: Literal["legacy", "v2"] = "legacy"
    work_units_used: int = Field(default=0, ge=0)
    work_counters: dict[str, int] = Field(default_factory=dict)
    queue_peak: int = Field(default=0, ge=0)
    aux_visits_used: int = Field(default=0, ge=0)
    stop_reasons: tuple[str, ...] = ()
    unexplored_source_count_known: bool = True
    frontiers: tuple[AttackFrontier, ...]
    expansions_used: int = Field(ge=0)
    search_complete: bool
    truncated: bool
    depth_limit_reached: bool
    unexplored_source_count: int = Field(ge=0)


class _WorkLimitReached(Exception):
    pass


@dataclass
class _SearchWork:
    query: FrontierQuery
    used: int = 0
    auxiliary: int = 0
    queue_peak: int = 0
    main_queue_size: int = 0
    aux_queue_size: int = 0
    expansions: int = 0
    depth_limit_reached: bool = False
    stop_reason: str = ""
    counters: dict[str, int] = field(default_factory=dict)
    finished: list[AttackFrontier] = field(default_factory=list)
    sink_cache: _SinkCache | None = None

    def stop(self, reason: str):
        self.stop_reason = reason
        raise _WorkLimitReached(reason)

    def charge(self, stage: str, amount: int = 1):
        if self.used + amount > self.query.max_work_units:
            self.stop("MAX_WORK_UNITS")
        if stage.startswith("aux_") and self.auxiliary + amount > self.query.max_aux_visits:
            self.stop("MAX_AUX_VISITS")
        self.used += amount
        self.counters[stage] = self.counters.get(stage, 0) + amount
        if stage.startswith("aux_"):
            self.auxiliary += amount

    def queue(self, projected_size: int, *, auxiliary: bool = False):
        combined = projected_size + (self.main_queue_size if auxiliary else self.aux_queue_size)
        if combined > self.query.max_queue_states:
            self.stop("MAX_QUEUE_STATES")
        if auxiliary:
            self.aux_queue_size = projected_size
        else:
            self.main_queue_size = projected_size
        self.queue_peak = max(self.queue_peak, combined)


class _FinishedFrontiers(list):
    def __init__(self, work: _SearchWork):
        super().__init__()
        self.work = work

    def append(self, item):
        if len(self) >= self.work.query.max_frontiers:
            self.work.stop("MAX_FRONTIERS")
        self.work.charge("frontier_output")
        super().append(item)


class _SinkCache(dict):
    """Bounded operation-local cache, admission stops at capacity (no semantic eviction)."""

    def __init__(self, capacity: int, max_bytes: int = 1_048_576):
        super().__init__()
        self.capacity = capacity
        self.max_bytes = max_bytes
        self.used_bytes = 0
        self.admission_refused = False

    @staticmethod
    def _size(key, value):
        return len(key[0].encode("utf-8")) + 8 + (len(value.encode("utf-8")) if value else 0)

    def __setitem__(self, key, value):
        previous_size = self._size(key, self[key]) if key in self else 0
        next_size = self.used_bytes - previous_size + self._size(key, value)
        if (key not in self and len(self) >= self.capacity) or next_size > self.max_bytes:
            self.admission_refused = True
            return
        super().__setitem__(key, value)
        self.used_bytes = next_size


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
    evidence_ids: tuple[str, ...]
    continue_after_sink: bool
    blocked: bool = False


def _semantics(catalog: ExtensionCatalog, relation: Relation) -> RelationSemantics:
    registration = catalog.relation_types.get(relation.relation_type_id)
    return registration.semantics if registration is not None else RelationSemantics()


def _ordered_unique(*groups: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(item for group in groups for item in group))


def _guard_index(
    relations: tuple[Relation, ...], catalog: ExtensionCatalog, work: _SearchWork | None = None
) -> dict[str, tuple[Relation, ...]]:
    by_scope: dict[str, list[Relation]] = defaultdict(list)
    for relation in relations:
        if work is not None:
            work.charge("guard_index")
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
    work: _SearchWork | None = None,
) -> _PathState:
    guard_ids = list(path.guard_relation_ids)
    uncertain_ids = list(path.uncertain_guard_relation_ids)
    missing_ids = list(path.missing_reason_ids)
    requirement_ids = list(path.requirement_ids)
    unknown_ids = list(path.unknown_relation_ids)
    evidence_ids = list(path.evidence_ids)
    blocked = path.blocked

    for guard in guards.get(node_ref, ()):
        if work is not None:
            work.charge("guard_check")
        semantics = _semantics(catalog, guard)
        guard_contexts = set(guard.context_refs)
        if (
            guard_contexts
            and path.context_refs
            and not guard_contexts.intersection(path.context_refs)
        ):
            continue
        guard_ids.append(guard.relation_id)
        evidence_ids.extend(guard.evidence_ids)
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
        evidence_ids=tuple(sorted(set(evidence_ids))),
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


def _dominance_key(path: _PathState) -> tuple[object, ...]:
    """Only states with interchangeable grounding and future semantics compete.

    Equal tags do not imply equal sources. Unknown, blocked, guard and evidence
    state must not disappear when selecting a shorter representative path.
    """
    return (
        path.relation_ids[0],
        path.nodes[0],
        path.nodes[-1],
        path.context_refs,
        path.source_tags,
        path.mechanism_tags,
        path.sink_tags,
        path.blocked,
        path.influence_proven,
        path.continue_after_sink,
        frozenset(path.guard_relation_ids),
        frozenset(path.uncertain_guard_relation_ids),
        frozenset(path.missing_reason_ids),
        frozenset(path.requirement_ids),
        frozenset(path.unknown_relation_ids),
        frozenset(path.evidence_ids),
    )


def _dominates(left: _PathState, right: _PathState, work: _SearchWork | None = None) -> bool:
    """A representative must allow every simple-path suffix of the other state.

    Retain incomparable visited sets. Strict ordering prevents mutual removal
    and is shared by enqueue and dequeue, including equal-length ties.
    """
    if work is not None:
        work.charge("dominance_check")
    if _dominance_key(left) != _dominance_key(right):
        return False
    left_nodes, right_nodes = frozenset(left.nodes), frozenset(right.nodes)
    if len(left.relation_ids) > len(right.relation_ids) or not left_nodes <= right_nodes:
        return False
    return (
        len(left.relation_ids) < len(right.relation_ids)
        or left_nodes < right_nodes
        or left.relation_ids < right.relation_ids
    )


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
    work: _SearchWork | None = None,
) -> str | None:
    if work is not None:
        work.queue(1, auxiliary=True)
    queue: deque[tuple[str, int]] = deque([(start_ref, 0)])
    visited = {start_ref}
    while queue:
        if work is not None:
            work.charge("aux_pop")
        node_ref, depth = queue.popleft()
        if work is not None:
            work.queue(len(queue), auxiliary=True)
        if depth >= max_depth:
            continue
        for relation in adjacency.get(node_ref, []):
            if work is not None:
                work.charge("aux_edge")
            semantics = _semantics(catalog, relation)
            if semantics.sink_tags:
                if work is not None:
                    work.queue(0, auxiliary=True)
                return relation.target_ref
            if relation.target_ref not in visited:
                if work is not None:
                    work.queue(len(queue) + 1, auxiliary=True)
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
    sink_cache: dict[tuple[str, int], str | None] | None = None,
    work: _SearchWork | None = None,
) -> AttackFrontier:
    complete = bool(path.sink_tags) if complete is None else complete
    uncertainty = bool(
        path.unknown_relation_ids or path.uncertain_guard_relation_ids or path.missing_reason_ids
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

    next_ids_list = []
    for relation in adjacency.get(path.nodes[-1], []):
        if work is not None:
            work.charge("hint_edge")
        if relation.target_ref not in path.nodes:
            next_ids_list.append(relation.relation_id)
            if len(next_ids_list) == 8:
                break
    next_ids = tuple(next_ids_list)
    potential_sink = None
    if not complete:
        sink_key = (path.nodes[-1], query.max_depth)
        if sink_cache is not None and sink_key in sink_cache:
            potential_sink = sink_cache[sink_key]
        else:
            if work is None:
                potential_sink = _nearest_sink(path.nodes[-1], adjacency, catalog, query.max_depth)
            else:
                potential_sink = _nearest_sink(
                    path.nodes[-1], adjacency, catalog, query.max_depth, work=work
                )
            if sink_cache is not None:
                sink_cache[sink_key] = potential_sink
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


def _search_frontiers(
    session: CoreSession,
    *,
    catalog: ExtensionCatalog,
    query: FrontierQuery | None = None,
    memoize_sink: bool = False,
    max_sink_cache_entries: int = 4096,
    max_sink_cache_bytes: int = 1_048_576,
    work: _SearchWork | None = None,
) -> FrontierSearchResult:
    """Run a deterministic bounded search over registered structural relation semantics."""

    if not catalog.frozen:
        raise RuntimeError("extension catalog must be frozen before Frontier search")
    query = query or FrontierQuery()
    sink_cache = _SinkCache(max_sink_cache_entries, max_sink_cache_bytes) if memoize_sink else None
    if work is not None:
        work.sink_cache = sink_cache
        # Admit the complete input before copying/sorting; never silently index a prefix.
        work.charge("relation_input", len(session.semantic.relations))
    relations = tuple(
        sorted(session.semantic.relations.values(), key=lambda item: item.relation_id)
    )
    traversable = tuple(
        relation for relation in relations if _semantics(catalog, relation).traversable
    )
    adjacency: dict[str, list[Relation]] = defaultdict(list)
    for relation in traversable:
        if work is not None:
            work.charge("adjacency_index")
        adjacency[relation.source_ref].append(relation)
    for values in adjacency.values():
        values.sort(key=lambda item: item.relation_id)

    guards = _guard_index(relations, catalog, work)
    if work is not None:
        work.charge("source_scan", len(traversable))
    sources = [relation for relation in traversable if _semantics(catalog, relation).source_tags]
    queue: deque[_PathState] = deque()
    for relation in sources:
        if work is not None:
            work.charge("source_initialization")
            work.queue(len(queue) + 1)
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
            influence_proven=(semantics.proves_influence and relation.verdict is TriState.TRUE),
            evidence_ids=tuple(sorted(set(relation.evidence_ids))),
            continue_after_sink=semantics.continue_after_sink,
            blocked=relation.verdict is TriState.FALSE,
        )
        queue.append(_apply_guards(path, relation.target_ref, guards, catalog, work))

    frontiers: list[AttackFrontier] = [] if work is None else work.finished
    expansions = 0
    depth_limit_reached = False
    source_ids = {path.relation_ids[0] for path in queue}
    # The number of inserted states is bounded by max_expansions.
    dominance: dict[tuple[object, ...], list[_PathState]] = {}

    while queue and expansions < query.max_expansions:
        if work is not None:
            if len(frontiers) >= query.max_frontiers:
                work.stop("MAX_FRONTIERS")
            work.charge("queue_pop")
        path = queue.popleft()
        if work is not None:
            work.queue(len(queue))
        if any(_dominates(other, path, work) for other in dominance.get(_dominance_key(path), ())):
            continue
        last_relation = session.semantic.relations[path.relation_ids[-1]]
        last_semantics = _semantics(catalog, last_relation)
        focused_sink_reached = bool(
            path.sink_tags and (not query.sink_hints or set(path.sink_tags) & set(query.sink_hints))
        )

        if path.blocked:
            frontiers.append(
                _to_frontier(
                    path,
                    query,
                    adjacency,
                    catalog,
                    complete=False,
                    sink_cache=sink_cache,
                    work=work,
                )
            )
            continue
        if focused_sink_reached:
            frontiers.append(
                _to_frontier(
                    path, query, adjacency, catalog, complete=True, sink_cache=sink_cache, work=work
                )
            )
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
                    sink_cache=sink_cache,
                    work=work,
                )
            )
            continue
        if len(path.relation_ids) >= query.max_depth:
            depth_limit_reached = True
            if work is not None:
                work.depth_limit_reached = True
            frontiers.append(
                _to_frontier(
                    path,
                    query,
                    adjacency,
                    catalog,
                    exhausted=True,
                    sink_cache=sink_cache,
                    work=work,
                )
            )
            continue

        compatible_extensions = 0
        for relation in adjacency.get(path.nodes[-1], []):
            if work is not None:
                work.charge("edge_check")
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
                evidence_ids=tuple(sorted(set(path.evidence_ids) | set(relation.evidence_ids))),
                continue_after_sink=semantics.continue_after_sink,
                blocked=path.blocked or relation.verdict is TriState.FALSE,
            )
            candidate = _apply_guards(candidate, relation.target_ref, guards, catalog, work)
            dominance_key = _dominance_key(candidate)
            previous = dominance.get(dominance_key, [])
            if any(_dominates(other, candidate, work) for other in previous):
                continue
            dominance[dominance_key] = [
                other for other in previous if not _dominates(candidate, other, work)
            ] + [candidate]
            if work is not None:
                work.queue(len(queue) + 1)
            queue.append(candidate)
            compatible_extensions += 1
            expansions += 1
            if work is not None:
                work.expansions = expansions
            if expansions >= query.max_expansions:
                break
        if compatible_extensions == 0:
            frontiers.append(
                _to_frontier(
                    path,
                    query,
                    adjacency,
                    catalog,
                    exhausted=True,
                    sink_cache=sink_cache,
                    work=work,
                )
            )

    truncated = bool(queue) or len(frontiers) > query.max_frontiers
    selected = _diverse_rank(frontiers, query.max_frontiers)
    queued_sources = {path.relation_ids[0] for path in queue if path.relation_ids}
    return FrontierSearchResult(
        sink_memo_capacity_reached=(sink_cache is not None and sink_cache.admission_refused),
        frontiers=selected,
        expansions_used=expansions,
        search_complete=not queue and not depth_limit_reached,
        truncated=truncated,
        depth_limit_reached=depth_limit_reached,
        unexplored_source_count=len(source_ids & queued_sources),
    )


@performance_span(
    PerformanceStage.FRONTIER, input_items=lambda session, **kwargs: len(session.semantic.relations)
)
def build_attack_frontiers(
    session: CoreSession,
    *,
    catalog: ExtensionCatalog,
    query: FrontierQuery | None = None,
    memoize_sink: bool | None = None,
    max_sink_cache_entries: int = 4096,
    max_sink_cache_bytes: int = 1_048_576,
) -> FrontierSearchResult:
    """Legacy expansion budget or explicit v2 graph-work admission and limits.

    A work unit is a counted graph operation, not elapsed time or allocated bytes.
    Bounded final result ranking is performed even after graph work is stopped.
    Memoization remains explicit opt-in: the release tail-latency gate withheld
    a default change. Omitted policy preserves both legacy and v2 behavior.
    """
    if type(max_sink_cache_entries) is not int or not 0 <= max_sink_cache_entries <= 100_000:
        raise ValueError("sink cache capacity out of bounds")
    if type(max_sink_cache_bytes) is not int or not 0 <= max_sink_cache_bytes <= 64 * 1024**2:
        raise ValueError("sink cache byte capacity out of bounds")
    query = FrontierQuery.model_validate((query or FrontierQuery()).model_dump(mode="python"))
    if memoize_sink is not None and type(memoize_sink) is not bool:
        raise ValueError("memoize_sink must be a boolean or None")
    if memoize_sink is None:
        memoize_sink = False
    work = _SearchWork(query) if query.budget_version == "v2" else None
    if work is not None:
        work.finished = _FinishedFrontiers(work)
    try:
        result = _search_frontiers(
            session,
            catalog=catalog,
            query=query,
            memoize_sink=memoize_sink,
            max_sink_cache_entries=max_sink_cache_entries,
            max_sink_cache_bytes=max_sink_cache_bytes,
            work=work,
        )
    except _WorkLimitReached:
        assert work is not None
        result = FrontierSearchResult(
            sink_memo_capacity_reached=(
                work.sink_cache is not None and work.sink_cache.admission_refused
            ),
            frontiers=_diverse_rank(work.finished, query.max_frontiers),
            expansions_used=work.expansions,
            search_complete=False,
            truncated=True,
            depth_limit_reached=work.depth_limit_reached,
            unexplored_source_count=0,
            unexplored_source_count_known=False,
        )
    if work is not None:
        reasons = [work.stop_reason] if work.stop_reason else []
        if result.depth_limit_reached:
            reasons.append("MAX_DEPTH")
        if result.truncated and not work.stop_reason:
            reasons.append(
                "MAX_EXPANSIONS"
                if result.expansions_used >= query.max_expansions
                else "MAX_FRONTIERS"
            )
        result = result.model_copy(
            update={
                "budget_version": "v2",
                "work_units_used": work.used,
                "work_counters": dict(sorted(work.counters.items())),
                "queue_peak": work.queue_peak,
                "aux_visits_used": work.auxiliary,
                "stop_reasons": tuple(dict.fromkeys(reasons)),
            }
        )
    return result
