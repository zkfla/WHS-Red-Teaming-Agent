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
from cicd_redteam_agent.domain.models import AnalysisState
from cicd_redteam_agent.domain.proposal_draft import indexed_relations

DEFAULT_PORTFOLIO_SIZE = 6


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
class PortfolioEntry:
    rank: int
    frontier: AttackFrontier
    score: FrontierCandidateScore

    def as_dict(self) -> dict[str, object]:
        payload = self.frontier.as_dict()
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

    def as_dict(self) -> dict[str, object]:
        return {
            "transition": self.transition.value,
            "reason": self.reason,
            "observation_ready": self.observation_ready,
            "empty_submission_allowed": self.empty_submission_allowed,
            "allowed_relation_ref_paths": [
                list(references) for references in self.allowed_relation_ref_paths
            ],
            "portfolio": [entry.as_dict() for entry in self.portfolio],
            "search_complete": self.search_complete,
            "truncated": self.truncated,
            "stop_reason": self.stop_reason,
        }


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
    unmodeled_unknowns = sum(item.kind is UnknownReasonKind.UNMODELED for item in frontier.missing)
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
    for frontier in result.frontiers:
        if frontier.disposition is FrontierDisposition.BLOCKED:
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
        PortfolioEntry(rank=index, frontier=frontier, score=score)
        for index, (frontier, score) in enumerate(selected, start=1)
    )


def decide_investigation_policy(
    result: FrontierSearchResult,
    state: AnalysisState,
    *,
    observation_ready: bool = False,
    runtime: InvestigationRuntimeState | None = None,
    portfolio_limit: int = DEFAULT_PORTFOLIO_SIZE,
    selected_portfolio: tuple[PortfolioEntry, ...] | None = None,
) -> InvestigationPolicy:
    portfolio = selected_portfolio or select_frontier_portfolio(
        result,
        state,
        limit=portfolio_limit,
    )
    ready = [
        entry for entry in portfolio if entry.frontier.proposal_ready and entry.score.grounding > 0
    ]
    local = [
        entry
        for entry in portfolio
        if not entry.frontier.proposal_ready
        and any(
            missing.kind is UnknownReasonKind.LOCAL_RESOLVABLE for missing in entry.frontier.missing
        )
    ]
    stop_reason = runtime.partial_stop_reason() if runtime is not None else None
    if stop_reason is not None:
        transition = InvestigationTransition.STOP_PARTIAL
        reason = f"runtime cannot safely continue investigation: {stop_reason}"
    elif observation_ready:
        transition = InvestigationTransition.OBSERVE
        reason = "a deterministic external observation task is ready"
    elif ready and not (
        local and max(item.score.total for item in local) > max(item.score.total for item in ready)
    ):
        transition = InvestigationTransition.SUBMIT
        reason = "the selected portfolio contains sufficiently grounded proposal-ready paths"
    elif local or not result.search_complete or result.truncated:
        transition = InvestigationTransition.INVESTIGATE
        reason = "higher-value local uncertainty or incomplete Frontier search remains"
    else:
        transition = InvestigationTransition.SUBMIT
        reason = "Frontier search is complete and no further locally resolvable path remains"

    allowed_paths = tuple(entry.frontier.relation_refs for entry in ready)
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
    )
