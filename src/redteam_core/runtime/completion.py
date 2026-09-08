"""Conservative completion assessment. A finish request is never proof by itself."""

from datetime import datetime

from redteam_core.investigation.coverage import TERMINAL_DISPOSITIONS
from redteam_core.investigation.priority import stale_priority_decision_roots
from redteam_core.knowledge.state import CoreSession, KnowledgeSlotStatus
from redteam_core.models.decision import (
    CompletionAssessment,
    CompletionStatus,
    ScopeCompletion,
)
from redteam_core.models.execution import CleanupStatus, ExperimentAttemptStatus
from redteam_core.models.priority import PriorityState


def assess_completion(
    session: CoreSession,
    *,
    scope: ScopeCompletion,
    priority: PriorityState | None = None,
    evaluation_time: datetime | None = None,
) -> CompletionAssessment:
    stale_decisions = ()
    if priority is not None and priority.selections:
        if evaluation_time is None:
            raise ValueError("priority-aware completion requires an explicit evaluation time")
        stale_decisions = stale_priority_decision_roots(session, priority, now=evaluation_time)
    unresolved = tuple(
        sorted(
            requirement_id
            for requirement_id in session.investigation.requirements
            if session.knowledge.slots.get(requirement_id) is None
            or session.knowledge.slots[requirement_id].status is not KnowledgeSlotStatus.CONSISTENT
        )
    )
    blocked = tuple(
        sorted(
            {
                entry.root_cause_key
                for entry in session.candidate_state.coverage_ledger.values()
                if entry.disposition.value == "BLOCKED_EXTERNAL"
            }
        )
    )
    nonterminal_roots = sorted(
        {
            candidate.root_cause.qualified_key
            for candidate in session.candidate_state.candidates.values()
            if candidate.disposition not in TERMINAL_DISPOSITIONS
        }
    )
    uncertain_effects = tuple(
        sorted(
            attempt.attempt_id
            for attempt in session.execution.attempts.values()
            if attempt.status is ExperimentAttemptStatus.UNCERTAIN
        )
    )
    cleanup_terminal = {
        outcome.cleanup_requirement_id: outcome.status
        for outcome in session.execution.cleanup_outcomes.values()
    }
    pending_experiments = tuple(
        sorted(
            attempt.attempt_id
            for attempt in session.execution.attempts.values()
            if not attempt.status.terminal
        )
    )
    cleanup_gaps = tuple(
        sorted(
            requirement_id
            for requirement_id in session.execution.cleanup_requirements
            if cleanup_terminal.get(requirement_id) is not CleanupStatus.SUCCESS
        )
    )
    reasons = []
    if stale_decisions:
        reasons.append("STALE_ROOT_DECISIONS")
    if not scope.acquisition_complete:
        reasons.append("ACQUISITION_SCOPE_INCOMPLETE")
    if not scope.search_complete or scope.search_truncated:
        reasons.append("SEARCH_SCOPE_INCOMPLETE")
    if not session.candidate_state.candidates and not session.candidate_state.coverage_ledger:
        reasons.append("EMPTY_CANDIDATE_SET_UNPROVEN")
    if unresolved:
        reasons.append("UNRESOLVED_REQUIREMENTS")
    if nonterminal_roots:
        reasons.append("NONTERMINAL_ROOTS")
    if blocked:
        reasons.append("BLOCKED_EXTERNAL_ROOTS")
    if uncertain_effects:
        reasons.append("UNCERTAIN_EFFECTS")
    if pending_experiments:
        reasons.append("PENDING_EXPERIMENTS")
    if session.execution.stop_reason:
        reasons.append("EXECUTION_POLICY_STOP")
    if cleanup_gaps:
        reasons.append("CLEANUP_GAPS")
    return CompletionAssessment(
        status=CompletionStatus.PARTIAL if reasons else CompletionStatus.COMPLETE,
        reasons=tuple(reasons),
        candidate_count=len(session.candidate_state.candidates),
        unresolved_requirement_ids=unresolved,
        blocked_root_keys=blocked,
        uncertain_effect_ids=uncertain_effects,
        pending_experiment_ids=pending_experiments,
        cleanup_gap_ids=cleanup_gaps,
        stale_decision_root_keys=stale_decisions,
    )
