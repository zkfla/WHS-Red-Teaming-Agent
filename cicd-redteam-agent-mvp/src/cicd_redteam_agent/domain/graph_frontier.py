from __future__ import annotations

import hashlib
from collections import defaultdict, deque
from dataclasses import dataclass, replace
from enum import StrEnum

from cicd_redteam_agent.domain.models import (
    AnalysisState,
    FindingStatus,
    JobExecutionContext,
    Relation,
    TriggerExecutionContext,
    TriState,
)
from cicd_redteam_agent.domain.proposal_draft import indexed_relations
from cicd_redteam_agent.domain.relation_semantics import (
    RELATION_SEMANTICS_VERSION,
    relation_semantics,
)

FRONTIER_ENGINE_VERSION = "1.0"
DEFAULT_MAX_DEPTH = 10
DEFAULT_MAX_FRONTIERS = 50
DEFAULT_MAX_EXPANSIONS = 5_000


class FrontierConnectivity(StrEnum):
    COMPLETE = "COMPLETE"
    PARTIAL = "PARTIAL"


class FrontierDisposition(StrEnum):
    READY = "READY"
    EXPANDABLE = "EXPANDABLE"
    BLOCKED = "BLOCKED"
    EXHAUSTED = "EXHAUSTED"


class GuardEffect(StrEnum):
    BLOCKS = "BLOCKS"
    MAY_BLOCK = "MAY_BLOCK"
    INFORMATIONAL = "INFORMATIONAL"


class UnknownReasonKind(StrEnum):
    LOCAL_RESOLVABLE = "LOCAL_RESOLVABLE"
    EXTERNAL_REQUIRED = "EXTERNAL_REQUIRED"
    UNMODELED = "UNMODELED"


@dataclass(frozen=True)
class FrontierQuery:
    source_hints: tuple[str, ...] = ()
    mechanism_hints: tuple[str, ...] = ()
    sink_hints: tuple[str, ...] = ()
    max_depth: int = DEFAULT_MAX_DEPTH
    max_frontiers: int = DEFAULT_MAX_FRONTIERS
    max_expansions: int = DEFAULT_MAX_EXPANSIONS

    @property
    def focused(self) -> bool:
        return bool(self.source_hints or self.mechanism_hints or self.sink_hints)


@dataclass(frozen=True)
class UnknownReason:
    kind: UnknownReasonKind
    description: str
    relation_ref: str | None = None

    def as_dict(self) -> dict[str, str | None]:
        return {
            "kind": self.kind.value,
            "description": self.description,
            "relation_ref": self.relation_ref,
        }


@dataclass(frozen=True)
class GuardAssessment:
    relation_ref: str
    effect: GuardEffect
    scope_node_id: str
    evidence_count: int
    missing_evidence: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "relation_ref": self.relation_ref,
            "effect": self.effect.value,
            "scope_node_id": self.scope_node_id,
            "evidence_count": self.evidence_count,
            "missing_evidence": list(self.missing_evidence),
        }


@dataclass(frozen=True)
class _PathState:
    nodes: tuple[str, ...]
    relation_refs: tuple[str, ...]
    relation_ids: tuple[str, ...]
    source_tag: str
    mechanism_tags: tuple[str, ...]
    flow_labels: tuple[str, ...]
    attacker_influence_proven: bool
    sink_tag: str | None
    trigger_context_id: str | None
    job_context_id: str | None
    permission_state: tuple[str, ...]
    trust_provenance: tuple[str, ...]
    missing: tuple[UnknownReason, ...]
    guards: tuple[GuardAssessment, ...]
    blocked: bool = False


@dataclass(frozen=True)
class AttackFrontier:
    frontier_id: str
    connectivity: FrontierConnectivity
    disposition: FrontierDisposition
    proposal_status_ceiling: FindingStatus
    proposal_ready: bool
    source_node_id: str
    current_node_id: str
    sink_node_id: str | None
    potential_sink_node_id: str | None
    source_tag: str
    mechanism_tags: tuple[str, ...]
    flow_labels: tuple[str, ...]
    sink_tag: str | None
    relation_refs: tuple[str, ...]
    trigger_context_id: str | None
    job_context_id: str | None
    permission_state: tuple[str, ...]
    trust_provenance: tuple[str, ...]
    guards: tuple[GuardAssessment, ...]
    missing: tuple[UnknownReason, ...]
    next_relation_refs: tuple[str, ...]
    objective_score: int

    def as_dict(self) -> dict[str, object]:
        return {
            "frontier_id": self.frontier_id,
            "connectivity": self.connectivity.value,
            "disposition": self.disposition.value,
            "proposal_status_ceiling": self.proposal_status_ceiling.value,
            "proposal_ready": self.proposal_ready,
            "source_node_id": self.source_node_id,
            "current_node_id": self.current_node_id,
            "sink_node_id": self.sink_node_id,
            "potential_sink_node_id": self.potential_sink_node_id,
            "source_tag": self.source_tag,
            "mechanism_tags": list(self.mechanism_tags),
            "flow_labels": list(self.flow_labels),
            "sink_tag": self.sink_tag,
            "relation_refs": list(self.relation_refs),
            "trigger_context_id": self.trigger_context_id,
            "job_context_id": self.job_context_id,
            "permission_state": list(self.permission_state),
            "trust_provenance": list(self.trust_provenance),
            "guards": [item.as_dict() for item in self.guards],
            "missing": [item.as_dict() for item in self.missing],
            "next_relation_refs": list(self.next_relation_refs),
            "objective_score": self.objective_score,
        }


@dataclass(frozen=True)
class FrontierSearchResult:
    frontiers: tuple[AttackFrontier, ...]
    expansions_used: int
    search_complete: bool
    truncated: bool
    depth_limit_reached: bool
    unexplored_source_count: int

    def as_dict(self) -> dict[str, object]:
        return {
            "frontiers": [item.as_dict() for item in self.frontiers],
            "total": len(self.frontiers),
            "expansions_used": self.expansions_used,
            "search_complete": self.search_complete,
            "truncated": self.truncated,
            "depth_limit_reached": self.depth_limit_reached,
            "unexplored_source_count": self.unexplored_source_count,
            "frontier_engine_version": FRONTIER_ENGINE_VERSION,
            "relation_semantics_version": RELATION_SEMANTICS_VERSION,
        }


_MAY_BLOCK_GUARDS = {
    "environment_protection_unknown",
    "fork_pr_workflow_requires_approval",
    "environment_requires_deployment_review",
    "environment_restricts_deployment_branches",
    "environment_custom_protection_rules_enabled",
    "environment_deployment_branch_policy_unknown",
    "branch_rules_may_block_direct_push",
}
_INFORMATIONAL_GUARDS = {
    "verifies_content_integrity",
    "validates_value_with_allowlist",
    "artifact_verified_by_integrity_guard",
    "environment_wait_timer_configured",
    "environment_has_no_deployment_protection",
}
_EXTERNAL_UNKNOWN_TERMS = (
    "trust policy",
    "environment protection",
    "repository default",
    "branch protection",
    "approval",
    "external dependency",
    "upstream compromise",
    "runner isolation",
    "runtime setting",
    "token availability",
)
_LOCAL_UNKNOWN_TERMS = (
    "referenced file",
    "local action",
    "local reusable",
    "script content",
    "dynamic path",
    "condition expression",
)


def _unknown_kind(description: str) -> UnknownReasonKind:
    lowered = description.casefold()
    if any(term in lowered for term in _EXTERNAL_UNKNOWN_TERMS):
        return UnknownReasonKind.EXTERNAL_REQUIRED
    if any(term in lowered for term in _LOCAL_UNKNOWN_TERMS):
        return UnknownReasonKind.LOCAL_RESOLVABLE
    return UnknownReasonKind.UNMODELED


def _merge_unknowns(*groups: tuple[UnknownReason, ...]) -> tuple[UnknownReason, ...]:
    merged: dict[tuple[UnknownReasonKind, str], UnknownReason] = {}
    for item in (item for group in groups for item in group):
        merged.setdefault((item.kind, item.description), item)
    return tuple(merged.values())


def _permission_state(context: JobExecutionContext | None) -> tuple[str, ...]:
    if context is None:
        return ()
    values = [
        f"{scope}:{level.value}"
        for scope, level in sorted(context.effective_permissions.levels.items())
    ]
    values.append(f"default:{context.effective_permissions.default_level.value}")
    return tuple(values)


def _trust_provenance(
    context: JobExecutionContext | None,
    trigger_contexts: dict[str, TriggerExecutionContext],
) -> tuple[str, ...]:
    if context is None:
        return ()
    trigger = trigger_contexts.get(context.trigger_context_id)
    values = list(context.effective_permissions.provenance)
    if trigger is not None:
        values.extend(
            (
                f"event:{trigger.event_name}",
                f"activation:{trigger.activation_type}",
                f"workflow_revision_trusted:{trigger.workflow_revision_trusted.value}",
            )
        )
    return tuple(dict.fromkeys(values))


def _context_indexes(
    state: AnalysisState,
) -> tuple[dict[str, JobExecutionContext], dict[str, set[str]], dict[str, str]]:
    job_contexts = {item.context_id: item for item in state.job_execution_contexts}
    node_jobs: dict[str, set[str]] = defaultdict(set)
    for item in state.job_execution_contexts:
        node_jobs[item.context_id].add(item.context_id)
        node_jobs[item.job_id].add(item.context_id)
    step_execution_to_job: dict[str, str] = {}
    for relation in state.relations:
        if (
            relation.relation_type == "job_reaches_step_execution"
            and relation.source_id in job_contexts
        ):
            step_execution_to_job[relation.target_id] = relation.source_id
            node_jobs[relation.target_id].add(relation.source_id)
    for relation in state.relations:
        if relation.relation_type == "executes_step_definition":
            job_context_id = step_execution_to_job.get(relation.source_id)
            if job_context_id:
                node_jobs[relation.target_id].add(job_context_id)
    trigger_for_job = {
        context_id: item.trigger_context_id for context_id, item in job_contexts.items()
    }
    return job_contexts, node_jobs, trigger_for_job


def _guard_index(
    indexed: list[tuple[str, Relation]], node_jobs: dict[str, set[str]]
) -> dict[str, list[GuardAssessment]]:
    result: dict[str, list[GuardAssessment]] = defaultdict(list)

    def add(node_id: str, assessment: GuardAssessment) -> None:
        result[node_id].append(assessment)
        for context_id in node_jobs.get(node_id, set()):
            result[context_id].append(replace(assessment, scope_node_id=context_id))

    for relation_ref, relation in indexed:
        if relation.relation_type == "guard_blocks_untrusted_execution":
            if relation.verdict is TriState.FALSE:
                continue
            add(
                relation.target_id,
                GuardAssessment(
                    relation_ref=relation_ref,
                    effect=(
                        GuardEffect.BLOCKS
                        if relation.verdict is TriState.TRUE
                        else GuardEffect.MAY_BLOCK
                    ),
                    scope_node_id=relation.target_id,
                    evidence_count=len(relation.evidence_ids),
                    missing_evidence=tuple(relation.missing_evidence),
                ),
            )
        elif relation.relation_type in _MAY_BLOCK_GUARDS:
            add(
                relation.source_id,
                GuardAssessment(
                    relation_ref=relation_ref,
                    effect=GuardEffect.MAY_BLOCK,
                    scope_node_id=relation.source_id,
                    evidence_count=len(relation.evidence_ids),
                    missing_evidence=tuple(relation.missing_evidence),
                ),
            )
        elif relation.relation_type in _INFORMATIONAL_GUARDS:
            add(
                relation.source_id,
                GuardAssessment(
                    relation_ref=relation_ref,
                    effect=GuardEffect.INFORMATIONAL,
                    scope_node_id=relation.source_id,
                    evidence_count=len(relation.evidence_ids),
                    missing_evidence=tuple(relation.missing_evidence),
                ),
            )
    for values in result.values():
        values.sort(key=lambda item: item.relation_ref)
    return result


def _guard_unknowns(guards: tuple[GuardAssessment, ...]) -> tuple[UnknownReason, ...]:
    unknowns: list[UnknownReason] = []
    for guard in guards:
        if guard.effect is not GuardEffect.MAY_BLOCK:
            continue
        descriptions = guard.missing_evidence or (
            f"guard effectiveness unresolved for {guard.scope_node_id}",
        )
        unknowns.extend(
            UnknownReason(_unknown_kind(item), item, guard.relation_ref) for item in descriptions
        )
    return tuple(unknowns)


def _frontier_id(path: _PathState) -> str:
    digest = hashlib.sha256(
        "|".join(
            (
                *path.relation_ids,
                path.trigger_context_id or "",
                path.job_context_id or "",
            )
        ).encode()
    ).hexdigest()[:16]
    return f"frontier_{digest}"


def _objective_score(path: _PathState, query: FrontierQuery) -> int:
    score = 0
    if path.source_tag in query.source_hints:
        score += 6
    score += 5 * len(set(path.mechanism_tags) & set(query.mechanism_hints))
    if path.sink_tag and path.sink_tag in query.sink_hints:
        score += 7
    if path.sink_tag:
        score += 3
    if path.blocked:
        score -= 2
    score -= len(path.missing)
    return score


def _source_tag(relation: Relation, default: str | None) -> str:
    if relation.source_id == "attacker:fork-pr":
        return "fork_pr"
    if relation.source_id == "attacker:repo-writer":
        return "repository_writer"
    return default or "unknown"


def _nearest_sink(
    start: str,
    adjacency: dict[str, list[tuple[str, Relation]]],
    max_depth: int,
) -> str | None:
    queue: deque[tuple[str, int]] = deque([(start, 0)])
    visited = {start}
    while queue:
        node, depth = queue.popleft()
        if depth >= max_depth:
            continue
        for _, relation in adjacency.get(node, []):
            semantics = relation_semantics(relation.relation_type)
            if semantics.sink_tag:
                return relation.target_id
            if relation.target_id not in visited:
                visited.add(relation.target_id)
                queue.append((relation.target_id, depth + 1))
    return None


def _to_frontier(
    path: _PathState,
    query: FrontierQuery,
    adjacency: dict[str, list[tuple[str, Relation]]],
    *,
    complete: bool | None = None,
    exhausted: bool = False,
) -> AttackFrontier:
    complete = path.sink_tag is not None if complete is None else complete
    if path.blocked:
        disposition = FrontierDisposition.BLOCKED
        ceiling = FindingStatus.REJECTED
    elif complete:
        disposition = (
            FrontierDisposition.READY
            if path.attacker_influence_proven
            else FrontierDisposition.EXHAUSTED
        )
        ceiling = (
            FindingStatus.CANDIDATE
            if path.missing or not path.attacker_influence_proven
            else FindingStatus.SUPPORTED
        )
    elif exhausted:
        disposition = FrontierDisposition.EXHAUSTED
        ceiling = FindingStatus.CANDIDATE
    else:
        disposition = FrontierDisposition.EXPANDABLE
        ceiling = FindingStatus.CANDIDATE
    next_refs = tuple(
        ref
        for ref, relation in adjacency.get(path.nodes[-1], [])
        if relation.target_id not in path.nodes
    )[:8]
    potential_sink = None if complete else _nearest_sink(path.nodes[-1], adjacency, query.max_depth)
    return AttackFrontier(
        frontier_id=_frontier_id(path),
        connectivity=(FrontierConnectivity.COMPLETE if complete else FrontierConnectivity.PARTIAL),
        disposition=disposition,
        proposal_status_ceiling=ceiling,
        proposal_ready=complete and not path.blocked and path.attacker_influence_proven,
        source_node_id=path.nodes[0],
        current_node_id=path.nodes[-1],
        sink_node_id=path.nodes[-1] if complete else None,
        potential_sink_node_id=potential_sink,
        source_tag=path.source_tag,
        mechanism_tags=path.mechanism_tags,
        flow_labels=path.flow_labels,
        sink_tag=path.sink_tag,
        relation_refs=path.relation_refs,
        trigger_context_id=path.trigger_context_id,
        job_context_id=path.job_context_id,
        permission_state=path.permission_state,
        trust_provenance=path.trust_provenance,
        guards=path.guards,
        missing=path.missing,
        next_relation_refs=next_refs,
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
            len(item.relation_refs),
            item.frontier_id,
        ),
    )
    buckets: dict[tuple[str, str | None, str | None, str | None], deque[AttackFrontier]] = {}
    for item in ordered:
        key = (
            item.source_tag,
            item.sink_tag,
            item.mechanism_tags[0] if item.mechanism_tags else None,
            item.trigger_context_id,
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
    state: AnalysisState, query: FrontierQuery | None = None
) -> FrontierSearchResult:
    """Build bounded, context-sensitive investigation candidates without findings."""
    query = query or FrontierQuery()
    indexed = [
        (reference, relation)
        for reference, relation in indexed_relations(state)
        if relation.category is not None and relation_semantics(relation.relation_type).traversable
    ]
    adjacency: dict[str, list[tuple[str, Relation]]] = defaultdict(list)
    for reference, relation in indexed:
        adjacency[relation.source_id].append((reference, relation))
    for values in adjacency.values():
        values.sort(key=lambda pair: pair[0])

    job_contexts, node_jobs, trigger_for_job = _context_indexes(state)
    trigger_contexts = {item.context_id: item for item in state.trigger_contexts}
    guards_by_node = _guard_index(indexed_relations(state), node_jobs)
    source_edges = [
        (reference, relation)
        for reference, relation in indexed
        if relation_semantics(relation.relation_type).source_tag is not None
    ]
    source_edges.sort(key=lambda pair: pair[0])

    queue: deque[_PathState] = deque()
    for reference, relation in source_edges:
        semantics = relation_semantics(relation.relation_type)
        target_contexts = sorted(node_jobs.get(relation.target_id, set())) or [None]
        for job_context_id in target_contexts:
            context = job_contexts.get(job_context_id) if job_context_id else None
            missing = tuple(
                UnknownReason(_unknown_kind(item), item, reference)
                for item in relation.missing_evidence
            )
            if relation.verdict is TriState.UNKNOWN and not missing:
                missing = (
                    UnknownReason(
                        UnknownReasonKind.UNMODELED,
                        "relation verdict is UNKNOWN without a specific reason",
                        reference,
                    ),
                )
            path_guards = tuple(guards_by_node.get(relation.target_id, []))
            missing = _merge_unknowns(missing, _guard_unknowns(path_guards))
            queue.append(
                _PathState(
                    nodes=(relation.source_id, relation.target_id),
                    relation_refs=(reference,),
                    relation_ids=(relation.relation_id,),
                    source_tag=_source_tag(relation, semantics.source_tag),
                    mechanism_tags=semantics.mechanism_tags,
                    flow_labels=tuple(
                        dict.fromkeys(
                            (
                                _source_tag(relation, semantics.source_tag),
                                *semantics.mechanism_tags,
                            )
                        )
                    ),
                    attacker_influence_proven=(
                        relation.relation_type != "can_trigger_event_context"
                    ),
                    sink_tag=semantics.sink_tag,
                    trigger_context_id=(
                        trigger_for_job.get(job_context_id) if job_context_id else None
                    ),
                    job_context_id=job_context_id,
                    permission_state=_permission_state(context),
                    trust_provenance=_trust_provenance(context, trigger_contexts),
                    missing=missing,
                    guards=path_guards,
                    blocked=(
                        relation.verdict is TriState.FALSE
                        or any(item.effect is GuardEffect.BLOCKS for item in path_guards)
                    ),
                )
            )

    frontiers: list[AttackFrontier] = []
    expansions = 0
    depth_limit_reached = False
    source_refs = {item.relation_refs[0] for item in queue}
    dominance: dict[
        tuple[str, str | None, str | None, tuple[str, ...], str | None],
        tuple[int, frozenset[tuple[UnknownReasonKind, str]]],
    ] = {}
    while queue and expansions < query.max_expansions:
        path = queue.popleft()
        focused_sink_reached = bool(
            path.sink_tag and (not query.sink_hints or path.sink_tag in query.sink_hints)
        )
        can_continue_past_intermediate_sink = bool(
            path.sink_tag
            and query.sink_hints
            and path.sink_tag not in query.sink_hints
            and adjacency.get(path.nodes[-1])
        )
        if path.blocked or focused_sink_reached:
            frontiers.append(_to_frontier(path, query, adjacency, complete=focused_sink_reached))
            continue
        if path.sink_tag and not can_continue_past_intermediate_sink:
            frontiers.append(_to_frontier(path, query, adjacency, complete=True))
            continue
        if len(path.relation_refs) >= query.max_depth:
            depth_limit_reached = True
            frontiers.append(_to_frontier(path, query, adjacency, exhausted=True))
            continue

        compatible_extensions = 0
        for reference, relation in adjacency.get(path.nodes[-1], []):
            if relation.target_id in path.nodes:
                continue
            semantics = relation_semantics(relation.relation_type)
            target_contexts = sorted(node_jobs.get(relation.target_id, set())) or [None]
            for target_context_id in target_contexts:
                if (
                    path.job_context_id
                    and target_context_id
                    and path.job_context_id != target_context_id
                    and not semantics.context_bridge
                ):
                    continue
                next_job_context_id = (
                    target_context_id
                    if semantics.context_bridge or path.job_context_id is None
                    else path.job_context_id
                )
                context = job_contexts.get(next_job_context_id) if next_job_context_id else None
                relation_missing = tuple(
                    UnknownReason(_unknown_kind(item), item, reference)
                    for item in relation.missing_evidence
                )
                if relation.verdict is TriState.UNKNOWN and not relation_missing:
                    relation_missing = (
                        UnknownReason(
                            UnknownReasonKind.UNMODELED,
                            "relation verdict is UNKNOWN without a specific reason",
                            reference,
                        ),
                    )
                node_guards = tuple(guards_by_node.get(relation.target_id, []))
                all_guards = tuple(dict.fromkeys((*path.guards, *node_guards)))
                combined_missing = _merge_unknowns(
                    path.missing,
                    relation_missing,
                    _guard_unknowns(node_guards),
                )
                candidate = replace(
                    path,
                    nodes=(*path.nodes, relation.target_id),
                    relation_refs=(*path.relation_refs, reference),
                    relation_ids=(*path.relation_ids, relation.relation_id),
                    mechanism_tags=tuple(
                        dict.fromkeys((*path.mechanism_tags, *semantics.mechanism_tags))
                    ),
                    flow_labels=tuple(
                        dict.fromkeys(
                            (
                                *path.flow_labels,
                                *semantics.mechanism_tags,
                                *((semantics.sink_tag,) if semantics.sink_tag else ()),
                            )
                        )
                    ),
                    sink_tag=semantics.sink_tag or path.sink_tag,
                    trigger_context_id=(
                        trigger_for_job.get(next_job_context_id)
                        if next_job_context_id
                        else path.trigger_context_id
                    ),
                    job_context_id=next_job_context_id,
                    permission_state=_permission_state(context) or path.permission_state,
                    trust_provenance=(
                        _trust_provenance(context, trigger_contexts)
                        if context
                        else path.trust_provenance
                    ),
                    missing=combined_missing,
                    guards=all_guards,
                    blocked=(
                        relation.verdict is TriState.FALSE
                        or any(item.effect is GuardEffect.BLOCKS for item in node_guards)
                    ),
                )
                dominance_key = (
                    candidate.nodes[-1],
                    candidate.trigger_context_id,
                    candidate.job_context_id,
                    candidate.mechanism_tags,
                    candidate.sink_tag,
                )
                missing_key = frozenset((item.kind, item.description) for item in candidate.missing)
                previous = dominance.get(dominance_key)
                if (
                    previous
                    and previous[0] <= len(candidate.relation_refs)
                    and previous[1] <= missing_key
                ):
                    continue
                dominance[dominance_key] = (len(candidate.relation_refs), missing_key)
                queue.append(candidate)
                compatible_extensions += 1
                expansions += 1
                if expansions >= query.max_expansions:
                    break
            if expansions >= query.max_expansions:
                break
        if compatible_extensions == 0:
            frontiers.append(_to_frontier(path, query, adjacency, exhausted=True))

    truncated = bool(queue) or len(frontiers) > query.max_frontiers
    selected = _diverse_rank(frontiers, query.max_frontiers)
    queued_source_refs = {item.relation_refs[0] for item in queue if item.relation_refs}
    return FrontierSearchResult(
        frontiers=selected,
        expansions_used=expansions,
        search_complete=not queue and not depth_limit_reached,
        truncated=truncated,
        depth_limit_reached=depth_limit_reached,
        unexplored_source_count=len(source_refs & queued_source_refs),
    )
