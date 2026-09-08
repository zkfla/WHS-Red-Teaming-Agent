from __future__ import annotations

from pydantic import Field, model_validator

from redteam_core.knowledge.state import CandidateState, CoreSession, CoverageLedgerEntry
from redteam_core.models.base import SchemaModel
from redteam_core.models.security import (
    CandidateDisposition,
    ClaimStatus,
    SecurityCandidate,
)

DEFAULT_PORTFOLIO_SIZE = 6
TERMINAL_DISPOSITIONS = frozenset(
    {
        CandidateDisposition.ACCEPTED,
        CandidateDisposition.REJECTED,
        CandidateDisposition.BLOCKED_EXTERNAL,
        CandidateDisposition.MERGED,
    }
)


class CandidateScore(SchemaModel):
    candidate_id: str = Field(pattern=r"^cand_[0-9a-f]{16}$")
    total: int
    readiness: int
    grounding: int
    information_gain: int
    cost: int


class PortfolioEntry(SchemaModel):
    rank: int = Field(ge=1)
    candidate_id: str = Field(pattern=r"^cand_[0-9a-f]{16}$")
    root_cause_key: str = Field(min_length=1, max_length=900)
    root_cause_family: str = Field(min_length=1, max_length=400)
    score: CandidateScore


class CandidatePortfolio(SchemaModel):
    candidate_revision: int = Field(ge=0)
    knowledge_revision: int = Field(ge=0)
    entries: tuple[PortfolioEntry, ...]

    @model_validator(mode="after")
    def validate_unique_roots(self) -> CandidatePortfolio:
        roots = [entry.root_cause_key for entry in self.entries]
        if len(roots) != len(set(roots)):
            raise ValueError("portfolio must contain at most one representative per root cause")
        return self


class RootCauseDecision(SchemaModel):
    root_cause_key: str = Field(min_length=1, max_length=900)
    disposition: CandidateDisposition
    reason: str = Field(min_length=1, max_length=1_000)

    @model_validator(mode="after")
    def require_terminal_disposition(self) -> RootCauseDecision:
        if self.disposition not in TERMINAL_DISPOSITIONS:
            raise ValueError("root-cause batch decisions must be terminal")
        return self


def _qualified_family(candidate: SecurityCandidate) -> str:
    root = candidate.root_cause
    return f"{root.owner_bundle_id}:{root.family_id}"


def score_security_candidate(candidate: SecurityCandidate) -> CandidateScore:
    readiness = {
        ClaimStatus.CONFIRMED: 80,
        ClaimStatus.SUPPORTED: 60,
        ClaimStatus.CANDIDATE: 40,
        ClaimStatus.REJECTED: -80,
    }[candidate.current_claim_status]
    readiness += {
        CandidateDisposition.UNREVIEWED: 0,
        CandidateDisposition.INVESTIGATING: -4,
        CandidateDisposition.ACCEPTED: -60,
        CandidateDisposition.REJECTED: -60,
        CandidateDisposition.BLOCKED_EXTERNAL: -55,
        CandidateDisposition.MERGED: -65,
    }[candidate.disposition]
    grounding = min(len(candidate.evidence_ids) * 3, 24) + min(
        len(candidate.supporting_relation_ids) * 2, 20
    )
    information_gain = min(len(candidate.requirement_ids) * 5, 30)
    cost = -(len(candidate.supporting_relation_ids) + 2 * len(candidate.requirement_ids))
    return CandidateScore(
        candidate_id=candidate.candidate_id,
        total=readiness + grounding + information_gain + cost,
        readiness=readiness,
        grounding=grounding,
        information_gain=information_gain,
        cost=cost,
    )


def _groups(session: CoreSession) -> dict[str, list[SecurityCandidate]]:
    groups: dict[str, list[SecurityCandidate]] = {}
    for candidate in session.candidate_state.candidates.values():
        groups.setdefault(candidate.root_cause.qualified_key, []).append(candidate)
    return groups


def _root_terminal(group: list[SecurityCandidate]) -> bool:
    return bool(group) and all(item.disposition in TERMINAL_DISPOSITIONS for item in group)


def _validate_candidate_state(session: CoreSession) -> None:
    session.candidate_state = CandidateState.model_validate(
        session.candidate_state.model_dump(mode="python")
    )


def coverage_complete(session: CoreSession) -> bool:
    groups = _groups(session)
    return not groups or all(_root_terminal(group) for group in groups.values())


def reconcile_candidate_state(
    session: CoreSession,
    current: tuple[SecurityCandidate, ...],
) -> None:
    """Replace the current candidate set while preserving durable terminal investigation state."""

    previous = dict(session.candidate_state.candidates)
    ledger = dict(session.candidate_state.coverage_ledger)
    next_candidates: dict[str, SecurityCandidate] = {}

    for raw_candidate in sorted(current, key=lambda item: item.candidate_id):
        candidate = SecurityCandidate.model_validate(raw_candidate.model_dump(mode="python"))
        old = previous.get(candidate.candidate_id)
        if candidate.current_claim_status is ClaimStatus.REJECTED:
            candidate = candidate.model_copy(
                update={
                    "disposition": CandidateDisposition.REJECTED,
                    "disposition_reason": "deterministic semantic recompute rejected candidate",
                }
            )
        elif old is not None and old.disposition is not CandidateDisposition.UNREVIEWED:
            candidate = candidate.model_copy(
                update={
                    "disposition": old.disposition,
                    "disposition_reason": old.disposition_reason,
                }
            )
        next_candidates[candidate.candidate_id] = candidate
        old_entry = ledger.get(candidate.candidate_id)
        ledger[candidate.candidate_id] = CoverageLedgerEntry(
            item_id=candidate.candidate_id,
            candidate_id=candidate.candidate_id,
            root_cause_key=candidate.root_cause.qualified_key,
            disposition=candidate.disposition,
            selection_count=old_entry.selection_count if old_entry else 0,
            last_selected_batch=old_entry.last_selected_batch if old_entry else None,
            last_seen_knowledge_revision=session.knowledge.revision,
            active=True,
            reason=candidate.disposition_reason or (old_entry.reason if old_entry else None),
        )

    for candidate_id, old in previous.items():
        if candidate_id in next_candidates:
            continue
        old_entry = ledger.get(candidate_id)
        disposition = old.disposition
        reason = old.disposition_reason
        if disposition not in TERMINAL_DISPOSITIONS:
            disposition = CandidateDisposition.BLOCKED_EXTERNAL
            reason = "candidate disappeared or became unreachable after semantic recompute"
        ledger[candidate_id] = CoverageLedgerEntry(
            item_id=candidate_id,
            candidate_id=candidate_id,
            root_cause_key=old.root_cause.qualified_key,
            disposition=disposition,
            selection_count=old_entry.selection_count if old_entry else 0,
            last_selected_batch=old_entry.last_selected_batch if old_entry else None,
            last_seen_knowledge_revision=(
                old_entry.last_seen_knowledge_revision if old_entry else session.knowledge.revision
            ),
            active=False,
            reason=reason,
        )

    candidate_set_changed = next_candidates != previous
    session.candidate_state = CandidateState(
        revision=session.candidate_state.revision + (1 if candidate_set_changed else 0),
        candidates=next_candidates,
        coverage_ledger=dict(sorted(ledger.items())),
        batch_sequence=session.candidate_state.batch_sequence,
    )


def select_candidate_portfolio(
    session: CoreSession,
    *,
    limit: int = DEFAULT_PORTFOLIO_SIZE,
) -> CandidatePortfolio:
    if limit < 1:
        raise ValueError("portfolio limit must be positive")

    representatives: list[tuple[SecurityCandidate, CandidateScore]] = []
    for root_key, group in sorted(_groups(session).items()):
        if _root_terminal(group):
            continue
        eligible = [item for item in group if item.disposition not in TERMINAL_DISPOSITIONS]
        if not eligible:
            continue
        ranked = sorted(
            ((item, score_security_candidate(item)) for item in eligible),
            key=lambda pair: (-pair[1].total, pair[0].candidate_id),
        )
        representative, score = ranked[0]
        assert representative.root_cause.qualified_key == root_key
        representatives.append((representative, score))

    ordered = sorted(
        representatives,
        key=lambda pair: (-pair[1].total, pair[0].candidate_id),
    )
    selected: list[tuple[SecurityCandidate, CandidateScore]] = []
    selected_ids: set[str] = set()
    seen_families: set[str] = set()
    for candidate, score in ordered:
        family = _qualified_family(candidate)
        if family in seen_families:
            continue
        selected.append((candidate, score))
        selected_ids.add(candidate.candidate_id)
        seen_families.add(family)
        if len(selected) >= limit:
            break
    if len(selected) < limit:
        for candidate, score in ordered:
            if candidate.candidate_id in selected_ids:
                continue
            selected.append((candidate, score))
            selected_ids.add(candidate.candidate_id)
            if len(selected) >= limit:
                break

    return CandidatePortfolio(
        candidate_revision=session.candidate_state.revision,
        knowledge_revision=session.knowledge.revision,
        entries=tuple(
            PortfolioEntry(
                rank=index,
                candidate_id=candidate.candidate_id,
                root_cause_key=candidate.root_cause.qualified_key,
                root_cause_family=_qualified_family(candidate),
                score=score,
            )
            for index, (candidate, score) in enumerate(selected, start=1)
        )
    )


def record_portfolio_selection(session: CoreSession, portfolio: CandidatePortfolio) -> int:
    if portfolio.candidate_revision != session.candidate_state.revision:
        raise ValueError("portfolio candidate revision is stale")
    if portfolio.knowledge_revision != session.knowledge.revision:
        raise ValueError("portfolio knowledge revision is stale")
    if portfolio.entries and session.candidate_state.batch_sequence > 0:
        already_recorded = all(
            (
                session.candidate_state.coverage_ledger[entry.candidate_id].last_selected_batch
                == session.candidate_state.batch_sequence
            )
            for entry in portfolio.entries
        )
        if already_recorded:
            raise ValueError("portfolio is already recorded as the current investigation batch")
    session.candidate_state.batch_sequence += 1
    batch_id = session.candidate_state.batch_sequence
    for entry in portfolio.entries:
        ledger = session.candidate_state.coverage_ledger.get(entry.candidate_id)
        if ledger is None:
            raise ValueError("portfolio references candidate without coverage ledger entry")
        session.candidate_state.coverage_ledger[entry.candidate_id] = ledger.model_copy(
            update={
                "selection_count": ledger.selection_count + 1,
                "last_selected_batch": batch_id,
            }
        )
    _validate_candidate_state(session)
    return batch_id


def commit_root_cause_decisions(
    session: CoreSession,
    portfolio: CandidatePortfolio,
    decisions: tuple[RootCauseDecision, ...],
) -> None:
    if portfolio.candidate_revision != session.candidate_state.revision:
        raise ValueError("portfolio candidate revision is stale")
    if portfolio.knowledge_revision != session.knowledge.revision:
        raise ValueError("portfolio knowledge revision is stale")
    if session.candidate_state.batch_sequence < 1:
        raise ValueError("portfolio must be recorded before decisions are committed")
    for entry in portfolio.entries:
        ledger = session.candidate_state.coverage_ledger.get(entry.candidate_id)
        if ledger is None or ledger.last_selected_batch != session.candidate_state.batch_sequence:
            raise ValueError("portfolio is not bound to the current investigation batch")
    selected_roots = {entry.root_cause_key for entry in portfolio.entries}
    decision_by_root = {decision.root_cause_key: decision for decision in decisions}
    if len(decision_by_root) != len(decisions):
        raise ValueError("duplicate root-cause decision in one batch")
    if set(decision_by_root) != selected_roots:
        raise ValueError("each selected root cause requires exactly one terminal decision")

    for candidate_id, candidate in list(session.candidate_state.candidates.items()):
        decision = decision_by_root.get(candidate.root_cause.qualified_key)
        if decision is None:
            continue
        updated = SecurityCandidate.model_validate(
            candidate.model_copy(
                update={
                    "disposition": decision.disposition,
                    "disposition_reason": decision.reason,
                }
            ).model_dump(mode="python")
        )
        session.candidate_state.candidates[candidate_id] = updated
        ledger = session.candidate_state.coverage_ledger[candidate_id]
        session.candidate_state.coverage_ledger[candidate_id] = ledger.model_copy(
            update={
                "disposition": decision.disposition,
                "reason": decision.reason,
            }
        )
    session.candidate_state.revision += 1
    _validate_candidate_state(session)
