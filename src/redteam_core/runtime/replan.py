"""Deterministic canonical event hints, never an authority or an extra model call."""

from redteam_core.knowledge.state import CoreSession
from redteam_core.models.base import canonical_json, sha256_text
from redteam_core.models.decision import ReplanEvaluation, ReplanReason
from redteam_core.models.runtime import RuntimeBudgetLimits, RuntimeState


def evaluate_replan_events(
    session: CoreSession, runtime: RuntimeState, limits: RuntimeBudgetLimits
) -> ReplanEvaluation:
    ledger = runtime.operational_budget
    if ledger is None:
        raise ValueError("replan evaluation requires operational budget")
    requests = max(
        0,
        min(
            ledger.limits.max_requests - ledger.charged_requests,
            limits.max_requests - runtime.usage.requests
            if limits.max_requests is not None
            else ledger.limits.max_requests,
        ),
    )
    token_limits = [
        value
        for value in (
            ledger.limits.max_tokens - ledger.charged_tokens
            if ledger.limits.max_tokens is not None
            else None,
            limits.max_tokens
            - runtime.usage.tokens
            - ledger.charged_tokens
            + runtime.operational_accounted_tokens
            if limits.max_tokens is not None
            else None,
        )
        if value is not None
    ]
    tokens = max(0, min(token_limits)) if token_limits else None

    def hashed(value):
        return "sha256:" + sha256_text(canonical_json(value))

    evidence = hashed(
        {
            "observations": sorted(
                {
                    (
                        row.requirement_id,
                        row.payload.content_hash,
                        row.provenance.trust_class.value,
                        tuple(sorted(row.evidence_ids)),
                    )
                    for row in session.investigation.observations.values()
                }
            ),
            "expectations": sorted(session.expectations.expectations),
            "candidate_evidence": sorted(
                {
                    evidence_id
                    for row in session.candidate_state.candidates.values()
                    for evidence_id in row.evidence_ids
                }
            ),
        }
    )
    conflicts = sorted(
        key for key, slot in session.knowledge.slots.items() if slot.status.value == "CONFLICTING"
    )
    blocked = sorted(
        {
            row.root_cause_key
            for row in session.candidate_state.coverage_ledger.values()
            if row.active and row.disposition.value == "BLOCKED_EXTERNAL"
        }
    )
    previous = (
        runtime.progress.replan_evaluations[-1] if runtime.progress.replan_evaluations else None
    )
    reasons = []
    if previous and evidence != previous.evidence_hash:
        reasons.append(ReplanReason.NEW_EVIDENCE)
    if conflicts and (previous is None or hashed(conflicts) != previous.conflict_hash):
        reasons.append(ReplanReason.DEPENDENCY_CONFLICT)
    if blocked and (previous is None or hashed(blocked) != previous.blocked_hash):
        reasons.append(ReplanReason.BLOCKED_ROOT)
    if previous:
        percent = runtime.progress.policy.budget_drop_percent

        def dropped(old, new):
            return (
                old is not None
                and new is not None
                and old > 0
                and ((old - new) * 100 >= old * percent)
            )

        if dropped(previous.remaining_requests, requests) or dropped(
            previous.remaining_tokens, tokens
        ):
            reasons.append(ReplanReason.BUDGET_DROP)
    return ReplanEvaluation(
        step_index=runtime.progress.next_step_index,
        evidence_hash=evidence,
        conflict_hash=hashed(conflicts),
        blocked_hash=hashed(blocked),
        remaining_requests=requests,
        remaining_tokens=tokens,
        reasons=tuple(reasons),
    )
