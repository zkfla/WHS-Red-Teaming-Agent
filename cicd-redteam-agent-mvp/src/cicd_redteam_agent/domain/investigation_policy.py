from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from cicd_redteam_agent.domain.graph_frontier import (
    AttackFrontier,
    FrontierConnectivity,
    FrontierDisposition,
    FrontierSearchResult,
    GuardEffect,
    UnknownReasonKind,
)
from cicd_redteam_agent.domain.models import (
    AnalysisState,
    CandidateDisposition,
    CoverageLedgerEntry,
    FindingStatus,
    SecurityCandidate,
)
from cicd_redteam_agent.domain.proposal_draft import indexed_relations
from cicd_redteam_agent.domain.relation_semantics import relation_semantics

DEFAULT_PORTFOLIO_SIZE = 6

_TERMINAL_DISPOSITIONS = {
    CandidateDisposition.ACCEPTED,
    CandidateDisposition.REJECTED,
    CandidateDisposition.BLOCKED_EXTERNAL,
    CandidateDisposition.MERGED,
}


class InvestigationTransition(StrEnum):
    OBSERVE = "OBSERVE"
    INVESTIGATE = "INVESTIGATE"
    SUBMIT = "SUBMIT"
    STOP_PARTIAL = "STOP_PARTIAL"


@dataclass(frozen=True)
class InvestigationRuntimeState:
    limit_reached: bool = False
    limit_reason: str | None = None
    tokens_remaining: int | None = None
    predicted_next_round_tokens: int = 0
    repeated_calls: int = 0
    no_progress_rounds: int = 0

    def partial_stop_reason(self) -> str | None:
        if self.limit_reached:
            return self.limit_reason or "limit_reached"
        if (
            self.tokens_remaining is not None
            and self.predicted_next_round_tokens > 0
            and self.tokens_remaining < self.predicted_next_round_tokens
        ):
            return "insufficient_token_reserve"
        if self.repeated_calls >= 4 or self.no_progress_rounds >= 4:
            return "no_progress"
        return None


@dataclass(frozen=True)
class FrontierCandidateScore:
    frontier_id: str
    total: int
    readiness: int
    impact: int
    grounding: int
    information_gain: int
    objective_alignment: int
    cost: int

    def as_dict(self) -> dict[str, object]:
        return {
            "frontier_id": self.frontier_id,
            "total": self.total,
            "breakdown": {
                "readiness": self.readiness,
                "impact": self.impact,
                "grounding": self.grounding,
                "information_gain": self.information_gain,
                "objective_alignment": self.objective_alignment,
                "cost": self.cost,
            },
        }


@dataclass(frozen=True)
class SecurityCandidateScore:
    candidate_id: str
    total: int
    readiness: int
    impact: int
    grounding: int
    information_gain: int
    objective_alignment: int
    cost: int

    def as_dict(self) -> dict[str, object]:
        return {
            "candidate_id": self.candidate_id,
            "total": self.total,
            "breakdown": {
                "readiness": self.readiness,
                "impact": self.impact,
                "grounding": self.grounding,
                "information_gain": self.information_gain,
                "objective_alignment": self.objective_alignment,
                "cost": self.cost,
            },
        }


@dataclass(frozen=True)
class PortfolioEntry:
    rank: int
    score: FrontierCandidateScore | SecurityCandidateScore
    frontier: AttackFrontier | None = None
    candidate: SecurityCandidate | None = None

    @property
    def item_id(self) -> str:
        if self.candidate is not None:
            return self.candidate.candidate_id
        if self.frontier is not None:
            return self.frontier.frontier_id
        return "unknown"

    @property
    def relation_refs(self) -> tuple[str, ...]:
        if self.candidate is not None:
            return tuple(self.candidate.supporting_relation_ids)
        if self.frontier is not None:
            return tuple(self.frontier.relation_refs)
        return ()

    def as_dict(self, *, compact: bool = False) -> dict[str, object]:
        if self.candidate is not None:
            if compact:
                candidate = self.candidate
                candidate_payload = {
                    "candidate_id": candidate.candidate_id,
                    "candidate_kind": candidate.candidate_kind.value,
                    "root_cause_family": candidate.root_cause_family,
                    "root_cause_key": candidate.root_cause_key,
                    "workflow_id": candidate.workflow_id,
                    "job_id": candidate.job_id,
                    "step_id": candidate.step_id,
                    "context_id": candidate.context_id,
                    "status": candidate.status.value,
                    "proposal_status_ceiling": candidate.proposal_status_ceiling.value,
                    "disposition": candidate.disposition.value,
                    "supporting_relation_ids": candidate.supporting_relation_ids,
                    "missing_evidence": candidate.missing_evidence,
                    "evidence_requirements": [
                        item.model_dump(mode="json") for item in candidate.evidence_requirements
                    ],
                    "path_variants": [
                        {
                            "path_id": path.path_id,
                            "status": path.status.value,
                        }
                        for path in candidate.path_variants
                    ],
                    "claim_contract": {
                        "rule_id": candidate.claim_contract.rule_id,
                        "title": candidate.claim_contract.title,
                        "bounded_summary": candidate.claim_contract.bounded_summary,
                        "proposal_status_ceiling": (
                            candidate.claim_contract.proposal_status_ceiling.value
                        ),
                        "required_relation_types": (
                            candidate.claim_contract.required_relation_types
                        ),
                        "required_privileged_relation_types": (
                            candidate.claim_contract.required_privileged_relation_types
                        ),
                        "forbidden_claims": candidate.claim_contract.forbidden_claims,
                    },
                }
            else:
                candidate_payload = self.candidate.model_dump(mode="json")
            payload = {
                "item_type": "security_candidate",
                "candidate": candidate_payload,
            }
        elif self.frontier is not None:
            payload = {
                "item_type": "attack_frontier",
                **self.frontier.as_dict(),
            }
        else:
            payload = {"item_type": "unknown", "item_id": self.item_id}
        payload["portfolio_rank"] = self.rank
        payload["candidate_score"] = self.score.as_dict()
        return payload


@dataclass(frozen=True)
class InvestigationPolicy:
    transition: InvestigationTransition
    reason: str
    portfolio: tuple[PortfolioEntry, ...]
    observation_ready: bool
    empty_submission_allowed: bool
    allowed_relation_ref_paths: tuple[tuple[str, ...], ...]
    search_complete: bool
    truncated: bool
    stop_reason: str | None = None
    candidate_coverage_complete: bool = True
    decision_submission_allowed: bool = False
    focused: bool = False
    coverage_ledger: tuple[CoverageLedgerEntry, ...] = ()

    def as_dict(self, *, compact: bool = False) -> dict[str, object]:
        payload: dict[str, object] = {
            "transition": self.transition.value,
            "reason": self.reason,
            "observation_ready": self.observation_ready,
            "empty_submission_allowed": self.empty_submission_allowed,
            "allowed_relation_ref_paths": [
                list(references) for references in self.allowed_relation_ref_paths
            ],
            "portfolio": [entry.as_dict(compact=compact) for entry in self.portfolio],
            "search_complete": self.search_complete,
            "truncated": self.truncated,
            "stop_reason": self.stop_reason,
            "candidate_coverage_complete": self.candidate_coverage_complete,
            "decision_submission_allowed": self.decision_submission_allowed,
            "focused": self.focused,
        }
        if compact:
            disposition_counts: dict[str, int] = {}
            for entry in self.coverage_ledger:
                key = entry.disposition.value
                disposition_counts[key] = disposition_counts.get(key, 0) + 1
            payload["coverage_summary"] = {
                "items": len(self.coverage_ledger),
                "selected": sum(entry.selected for entry in self.coverage_ledger),
                "by_disposition": disposition_counts,
            }
        else:
            payload["coverage_ledger"] = [
                entry.model_dump(mode="json") for entry in self.coverage_ledger
            ]
        return payload


_SINK_IMPACT = {
    "cloud_identity": 30,
    "github_token_write": 26,
    "oidc_token": 22,
    "secret": 20,
}


def score_frontier(
    frontier: AttackFrontier,
    state: AnalysisState,
) -> FrontierCandidateScore:
    relation_by_ref = dict(indexed_relations(state))
    readiness = {
        FrontierDisposition.READY: 65,
        FrontierDisposition.EXPANDABLE: 24,
        FrontierDisposition.EXHAUSTED: -8,
        FrontierDisposition.BLOCKED: -100,
    }[frontier.disposition]
    if frontier.connectivity is FrontierConnectivity.COMPLETE:
        readiness += 25
    if frontier.proposal_ready:
        readiness += 35

    impact = _SINK_IMPACT.get(frontier.sink_tag or "", 12 if frontier.sink_tag else 0)
    evidence_ids = {
        evidence_id
        for reference in frontier.relation_refs
        if (relation := relation_by_ref.get(reference)) is not None
        for evidence_id in relation.evidence_ids
    }
    grounding = min(len(evidence_ids) * 3, 24)

    local_unknowns = sum(
        item.kind is UnknownReasonKind.LOCAL_RESOLVABLE for item in frontier.missing
    )
    external_unknowns = sum(
        item.kind is UnknownReasonKind.EXTERNAL_REQUIRED for item in frontier.missing
    )
    unmodeled_unknowns = sum(
        item.kind is UnknownReasonKind.UNMODELED for item in frontier.missing
    )
    information_gain = local_unknowns * 8 + external_unknowns * 2 - unmodeled_unknowns * 3
    objective_alignment = frontier.objective_score * 12
    may_block = sum(guard.effect is GuardEffect.MAY_BLOCK for guard in frontier.guards)
    cost = -(len(frontier.relation_refs) + len(frontier.missing) * 2 + may_block * 2)
    total = readiness + impact + grounding + information_gain + objective_alignment + cost
    return FrontierCandidateScore(
        frontier_id=frontier.frontier_id,
        total=total,
        readiness=readiness,
        impact=impact,
        grounding=grounding,
        information_gain=information_gain,
        objective_alignment=objective_alignment,
        cost=cost,
    )


_CANDIDATE_IMPACT = {
    "untrusted-privileged-execution": 32,
    "secret-log-flow": 28,
    "artifact-trust": 27,
    "self-hosted-state-persistence": 26,
    "revision-approval-binding": 24,
    "mutable-action-reference": 22,
    "broad-token-permission": 20,
    "shell-injection": 20,
    "known-action-advisory": 12,
    "secret-rotation-age": 10,
}


def _candidate_root_cause_family(candidate: SecurityCandidate) -> str:
    return candidate.root_cause_family or candidate.root_cause_key


def _candidate_groups(state: AnalysisState) -> dict[str, list[SecurityCandidate]]:
    groups: dict[str, list[SecurityCandidate]] = {}
    for candidate in state.candidates:
        groups.setdefault(candidate.root_cause_key, []).append(candidate)
    return groups


def _root_cause_is_terminal(candidates: list[SecurityCandidate]) -> bool:
    return bool(candidates) and all(
        candidate.disposition in _TERMINAL_DISPOSITIONS for candidate in candidates
    )


def _candidate_coverage_complete(state: AnalysisState) -> bool:
    groups = _candidate_groups(state)
    return not groups or all(_root_cause_is_terminal(group) for group in groups.values())


def score_security_candidate(
    candidate: SecurityCandidate,
    state: AnalysisState,
) -> SecurityCandidateScore:
    status_readiness = {
        "SUPPORTED": 60,
        "CANDIDATE": 42,
        "REJECTED": -40,
    }
    disposition_penalty = {
        CandidateDisposition.UNREVIEWED: 0,
        CandidateDisposition.INVESTIGATING: -4,
        CandidateDisposition.ACCEPTED: -48,
        CandidateDisposition.REJECTED: -48,
        CandidateDisposition.BLOCKED_EXTERNAL: -42,
        CandidateDisposition.MERGED: -50,
    }[candidate.disposition]
    readiness = status_readiness.get(candidate.status.value, 0) + disposition_penalty
    grounding = min(len(candidate.evidence_ids) * 3, 24)
    information_gain = min(len(candidate.missing_evidence) * 7, 28)
    objective_alignment = 0
    cost = -(len(candidate.supporting_relation_ids) + len(candidate.missing_evidence) * 2)
    # Keep the state argument part of the score contract and reward candidates grounded in it.
    if candidate.supporting_relation_ids:
        known = {relation.relation_id for relation in state.relations}
        grounding += min(
            len(set(candidate.supporting_relation_ids) & known),
            8,
        )
    total = (
        readiness
        + _CANDIDATE_IMPACT.get(_candidate_root_cause_family(candidate), 12)
        + grounding
        + information_gain
        + objective_alignment
        + cost
    )
    return SecurityCandidateScore(
        candidate_id=candidate.candidate_id,
        total=total,
        readiness=readiness,
        impact=_CANDIDATE_IMPACT.get(_candidate_root_cause_family(candidate), 12),
        grounding=grounding,
        information_gain=information_gain,
        objective_alignment=objective_alignment,
        cost=cost,
    )
def _overlap(left: AttackFrontier, right: AttackFrontier) -> int:
    left_refs = set(left.relation_refs)
    right_refs = set(right.relation_refs)
    union = left_refs | right_refs
    relation_overlap = 0 if not union else (100 * len(left_refs & right_refs)) // len(union)
    same_context = bool(
        left.job_context_id and right.job_context_id and left.job_context_id == right.job_context_id
    )
    same_shape = (
        left.source_tag == right.source_tag
        and left.sink_tag == right.sink_tag
        and left.mechanism_tags[:1] == right.mechanism_tags[:1]
    )
    return relation_overlap + (15 if same_context else 0) + (15 if same_shape else 0)


def _candidate_semantic_tags(
    candidate: SecurityCandidate,
    state: AnalysisState,
) -> tuple[set[str], set[str]]:
    relation_by_id = {relation.relation_id: relation for relation in state.relations}
    relation_types = set(candidate.claim_contract.required_relation_types)
    relation_types.update(
        relation_by_id[relation_id].relation_type
        for relation_id in (
            *candidate.supporting_relation_ids,
            *candidate.contradicting_relation_ids,
        )
        if relation_id in relation_by_id
    )
    relation_types.update(
        edge.relation_type
        for variant in candidate.path_variants
        for edge in variant.edges
    )
    semantics = [relation_semantics(relation_type) for relation_type in relation_types]
    mechanisms = {tag for item in semantics for tag in item.mechanism_tags}
    sinks = {item.sink_tag for item in semantics if item.sink_tag is not None}
    return mechanisms, sinks


def _candidate_overlap(
    left: SecurityCandidate,
    right: SecurityCandidate,
    state: AnalysisState,
) -> int:
    left_refs = set(left.supporting_relation_ids)
    right_refs = set(right.supporting_relation_ids)
    union = left_refs | right_refs
    relation_overlap = 0 if not union else (100 * len(left_refs & right_refs)) // len(union)
    same_root_instance = left.root_cause_key == right.root_cause_key
    same_root_family = _candidate_root_cause_family(left) == _candidate_root_cause_family(right)
    same_kind = left.candidate_kind == right.candidate_kind
    same_context = bool(left.context_id and left.context_id == right.context_id)
    left_mechanisms, left_sinks = _candidate_semantic_tags(left, state)
    right_mechanisms, right_sinks = _candidate_semantic_tags(right, state)
    same_mechanism = bool(left_mechanisms & right_mechanisms)
    same_sink = bool(left_sinks & right_sinks)
    return (
        relation_overlap
        + (45 if same_root_instance else 20 if same_root_family else 0)
        + (20 if same_kind else 0)
        + (15 if same_context else 0)
        + (15 if same_mechanism else 0)
        + (15 if same_sink else 0)
    )


def _candidate_frontier_overlap(
    candidate: SecurityCandidate,
    frontier: AttackFrontier,
    state: AnalysisState,
) -> int:
    relation_by_ref = dict(indexed_relations(state))
    frontier_ids = {
        relation_by_ref[reference].relation_id
        for reference in frontier.relation_refs
        if reference in relation_by_ref
    }
    candidate_ids = set(candidate.supporting_relation_ids)
    union = candidate_ids | frontier_ids
    relation_overlap = 0 if not union else (100 * len(candidate_ids & frontier_ids)) // len(union)
    same_context = bool(candidate.context_id and candidate.context_id == frontier.job_context_id)
    candidate_mechanisms, candidate_sinks = _candidate_semantic_tags(candidate, state)
    same_mechanism = bool(candidate_mechanisms & set(frontier.mechanism_tags))
    same_sink = bool(frontier.sink_tag and frontier.sink_tag in candidate_sinks)
    return (
        relation_overlap
        + (20 if same_context else 0)
        + (15 if same_mechanism else 0)
        + (15 if same_sink else 0)
    )


def _item_overlap(left: PortfolioEntry, right: PortfolioEntry, state: AnalysisState) -> int:
    if left.candidate is not None and right.candidate is not None:
        return _candidate_overlap(left.candidate, right.candidate, state)
    if left.frontier is not None and right.frontier is not None:
        return _overlap(left.frontier, right.frontier)
    if left.candidate is not None and right.frontier is not None:
        return _candidate_frontier_overlap(left.candidate, right.frontier, state)
    if left.frontier is not None and right.candidate is not None:
        return _candidate_frontier_overlap(right.candidate, left.frontier, state)
    return 0


def _candidate_ref_paths(
    candidate: SecurityCandidate,
    state: AnalysisState,
) -> tuple[tuple[str, ...], ...]:
    references = {
        relation.relation_id: short_ref
        for short_ref, relation in indexed_relations(state)
    }
    paths: list[tuple[str, ...]] = []
    for variant in candidate.path_variants:
        refs = tuple(
            references[edge.relation_id]
            for edge in variant.edges
            if edge.relation_id in references
        )
        if refs:
            paths.append(refs)
    return tuple(dict.fromkeys(paths))


def _sync_candidate_coverage(
    state: AnalysisState,
    portfolio: tuple[PortfolioEntry, ...],
) -> tuple[CoverageLedgerEntry, ...]:
    selected = {entry.item_id for entry in portfolio}
    existing = {entry.item_id: entry for entry in state.coverage_ledger}
    ledger: dict[str, CoverageLedgerEntry] = {}
    for candidate in state.candidates:
        old = existing.get(candidate.candidate_id)
        ledger[candidate.candidate_id] = CoverageLedgerEntry(
            item_id=candidate.candidate_id,
            candidate_id=candidate.candidate_id,
            disposition=candidate.disposition,
            selected=candidate.candidate_id in selected,
            reason=candidate.disposition_reason
            or (old.reason if old is not None else None),
        )
    for entry in portfolio:
        if entry.frontier is not None and entry.frontier.frontier_id not in ledger:
            old = existing.get(entry.frontier.frontier_id)
            ledger[entry.frontier.frontier_id] = CoverageLedgerEntry(
                item_id=entry.frontier.frontier_id,
                frontier_id=entry.frontier.frontier_id,
                disposition=(
                    old.disposition
                    if old is not None
                    else CandidateDisposition.UNREVIEWED
                ),
                selected=True,
                reason=old.reason if old is not None else None,
            )
    # Preserve terminal accounting for candidates that disappeared during semantic recompute.
    # Dropping this history would make a hard-blocked item look silently unreviewed on the next
    # policy refresh.
    for item_id, old in existing.items():
        ledger.setdefault(item_id, old)
    state.coverage_ledger = list(ledger.values())
    return tuple(state.coverage_ledger)


def select_frontier_portfolio(
    result: FrontierSearchResult,
    state: AnalysisState,
    *,
    limit: int = DEFAULT_PORTFOLIO_SIZE,
) -> tuple[PortfolioEntry, ...]:
    if limit < 1:
        raise ValueError("portfolio limit must be positive")
    unique: dict[
        tuple[str | None, tuple[str, ...]],
        tuple[AttackFrontier, FrontierCandidateScore],
    ] = {}
    ledger_by_id = {entry.item_id: entry for entry in state.coverage_ledger}
    for frontier in result.frontiers:
        if frontier.disposition is FrontierDisposition.BLOCKED:
            continue
        previous = ledger_by_id.get(frontier.frontier_id)
        if previous is not None and previous.disposition in _TERMINAL_DISPOSITIONS:
            continue
        score = score_frontier(frontier, state)
        key = (frontier.job_context_id, frontier.relation_refs)
        previous = unique.get(key)
        if (
            previous is None
            or score.total > previous[1].total
            or (score.total == previous[1].total and frontier.frontier_id < previous[0].frontier_id)
        ):
            unique[key] = (frontier, score)
    remaining = list(unique.values())
    selected: list[tuple[AttackFrontier, FrontierCandidateScore]] = []
    while remaining and len(selected) < limit:
        frontier, score = min(
            remaining,
            key=lambda item: (
                -(
                    item[1].total
                    - max(
                        (
                            _overlap(item[0], chosen_frontier) // 2
                            for chosen_frontier, _ in selected
                        ),
                        default=0,
                    )
                ),
                not item[0].proposal_ready,
                len(item[0].relation_refs),
                item[0].frontier_id,
            ),
        )
        selected.append((frontier, score))
        remaining = [item for item in remaining if item[0].frontier_id != frontier.frontier_id]
    return tuple(
        PortfolioEntry(rank=index, score=score, frontier=frontier)
        for index, (frontier, score) in enumerate(selected, start=1)
    )


def select_investigation_portfolio(
    result: FrontierSearchResult,
    state: AnalysisState,
    *,
    limit: int = DEFAULT_PORTFOLIO_SIZE,
) -> tuple[PortfolioEntry, ...]:
    """Select a bounded portfolio across semantic candidates and graph frontiers."""

    if limit < 1:
        raise ValueError("portfolio limit must be positive")
    candidate_entries: list[PortfolioEntry] = []
    for _root_cause_key, group in sorted(_candidate_groups(state).items()):
        if _root_cause_is_terminal(group):
            continue
        eligible = [
            candidate
            for candidate in group
            if candidate.disposition not in _TERMINAL_DISPOSITIONS
        ]
        if not eligible:
            continue
        representative = max(
            eligible,
            key=lambda candidate: (
                score_security_candidate(candidate, state).total,
                candidate.candidate_id,
            ),
        )
        candidate_entries.append(
            PortfolioEntry(
                rank=0,
                score=score_security_candidate(representative, state),
                candidate=representative,
            )
        )

    # Once every canonical root-cause instance has a terminal decision, broad-mode coverage is
    # complete. Frontiers are exploratory supplements to those bounded batches rather than a second
    # exhaustive universe that would force the model through every graph variant.
    if state.candidates and not candidate_entries:
        return ()
    frontier_entries = list(select_frontier_portfolio(result, state, limit=limit))
    candidate_relation_sets = [
        set(candidate.supporting_relation_ids) for candidate in state.candidates
    ]
    filtered_frontiers = []
    relation_by_ref = dict(indexed_relations(state))
    for entry in frontier_entries:
        if entry.frontier is None:
            continue
        frontier_ids = {
            relation_by_ref[reference].relation_id
            for reference in entry.frontier.relation_refs
            if reference in relation_by_ref
        }
        if any(
            frontier_ids and frontier_ids <= candidate_ids
            for candidate_ids in candidate_relation_sets
        ):
            continue
        filtered_frontiers.append(entry)
    remaining = [*candidate_entries, *filtered_frontiers]
    selected: list[PortfolioEntry] = []
    while remaining and len(selected) < limit:
        item = max(
            remaining,
            key=lambda entry: (
                entry.score.total
                - max(
                    (_item_overlap(entry, chosen, state) // 2 for chosen in selected),
                    default=0,
                ),
                entry.candidate is not None,
                entry.item_id,
            ),
        )
        selected.append(item)
        remaining = [entry for entry in remaining if entry.item_id != item.item_id]
    return tuple(
        PortfolioEntry(
            rank=index,
            score=entry.score,
            frontier=entry.frontier,
            candidate=entry.candidate,
        )
        for index, entry in enumerate(selected, start=1)
    )


def decide_investigation_policy(
    result: FrontierSearchResult,
    state: AnalysisState,
    *,
    observation_ready: bool = False,
    runtime: InvestigationRuntimeState | None = None,
    portfolio_limit: int = DEFAULT_PORTFOLIO_SIZE,
    selected_portfolio: tuple[PortfolioEntry, ...] | None = None,
    focused: bool = False,
) -> InvestigationPolicy:
    selection_limit = 1 if focused else portfolio_limit
    portfolio = selected_portfolio or (
        select_investigation_portfolio(result, state, limit=selection_limit)
        if state.candidates
        else select_frontier_portfolio(result, state, limit=selection_limit)
    )
    ready = [
        entry
        for entry in portfolio
        if (
            entry.frontier is not None
            and entry.frontier.proposal_ready
            and getattr(entry.score, "grounding", 0) > 0
        )
        or (
            entry.candidate is not None
            and entry.candidate.status is not FindingStatus.REJECTED
            and bool(entry.candidate.path_variants)
            and getattr(entry.score, "grounding", 0) > 0
        )
    ]
    local = [
        entry
        for entry in portfolio
        if entry.frontier is not None
        and not entry.frontier.proposal_ready
        and any(
            missing.kind is UnknownReasonKind.LOCAL_RESOLVABLE for missing in entry.frontier.missing
        )
    ]
    candidate_entries = [entry for entry in portfolio if entry.candidate is not None]
    candidate_coverage_complete = (
        all(
            entry.candidate is not None
            and entry.candidate.disposition in _TERMINAL_DISPOSITIONS
            for entry in candidate_entries
        )
        if focused
        else _candidate_coverage_complete(state)
    )
    coverage_ledger = _sync_candidate_coverage(state, portfolio)
    ledger_by_id = {entry.item_id: entry for entry in coverage_ledger}
    portfolio_coverage_complete = all(
        (
            entry.candidate is not None
            and entry.candidate.disposition in _TERMINAL_DISPOSITIONS
        )
        or (
            entry.frontier is not None
            and ledger_by_id.get(entry.frontier.frontier_id) is not None
            and ledger_by_id[entry.frontier.frontier_id].disposition in _TERMINAL_DISPOSITIONS
        )
        for entry in portfolio
    )
    stop_reason = runtime.partial_stop_reason() if runtime is not None else None
    decision_submission_allowed = False
    if stop_reason is not None:
        transition = InvestigationTransition.STOP_PARTIAL
        reason = f"runtime cannot safely continue investigation: {stop_reason}"
    elif observation_ready:
        transition = InvestigationTransition.OBSERVE
        reason = "a deterministic external observation task is ready"
    elif (
        ready
        and result.search_complete
        and not result.truncated
        and not (
            local
            and max(item.score.total for item in local)
            > max(item.score.total for item in ready)
        )
    ):
        if portfolio_coverage_complete and candidate_coverage_complete:
            transition = InvestigationTransition.SUBMIT
            reason = (
                "all eligible root-cause representatives are covered and the current "
                "portfolio is terminal"
            )
        else:
            transition = InvestigationTransition.INVESTIGATE
            decision_submission_allowed = True
            reason = (
                "the selected portfolio is grounded; commit terminal decisions for this batch "
                "before rotating to uncovered root-cause representatives"
            )
    elif local or not result.search_complete or result.truncated:
        transition = InvestigationTransition.INVESTIGATE
        reason = "higher-value local uncertainty or incomplete Frontier search remains"
    elif not candidate_coverage_complete:
        transition = InvestigationTransition.INVESTIGATE
        reason = "eligible root-cause representatives remain uncovered"
    else:
        transition = InvestigationTransition.SUBMIT
        reason = "Frontier search is complete and no further locally resolvable path remains"

    allowed_paths = tuple(
        reference_path
        for entry in ready
        for reference_path in (
            (entry.frontier.relation_refs,)
            if entry.frontier is not None
            else _candidate_ref_paths(entry.candidate, state) if entry.candidate is not None else ()
        )
    )
    return InvestigationPolicy(
        transition=transition,
        reason=reason,
        portfolio=portfolio,
        observation_ready=observation_ready,
        empty_submission_allowed=(transition is InvestigationTransition.SUBMIT and not ready),
        allowed_relation_ref_paths=allowed_paths,
        search_complete=result.search_complete,
        truncated=result.truncated,
        stop_reason=stop_reason,
        candidate_coverage_complete=candidate_coverage_complete,
        decision_submission_allowed=decision_submission_allowed,
        focused=focused,
        coverage_ledger=coverage_ledger,
    )
