from __future__ import annotations

from redteam_core.knowledge.state import CoreSession
from redteam_core.models.base import canonical_json, stable_id
from redteam_core.models.execution import (
    CleanupAttempt,
    CleanupAttemptStatus,
    CleanupOutcome,
    CleanupStatus,
)
from redteam_core.runtime.clock import RuntimeClock


def start_cleanup_attempt(
    session: CoreSession,
    *,
    cleanup_requirement_id: str,
    clock: RuntimeClock,
) -> CleanupAttempt:
    if cleanup_requirement_id not in session.execution.cleanup_requirements:
        raise ValueError("unknown cleanup requirement")
    candidate = session.execution.model_copy(deep=True)
    sequence = candidate.cleanup_attempt_sequence + 1
    attempt = CleanupAttempt(
        cleanup_attempt_id=stable_id("cleanup_attempt", cleanup_requirement_id, sequence),
        sequence=sequence,
        cleanup_requirement_id=cleanup_requirement_id,
        status=CleanupAttemptStatus.STARTED,
        started_at=clock.now(),
    )
    candidate.cleanup_attempt_sequence = sequence
    candidate.cleanup_attempts[attempt.cleanup_attempt_id] = attempt
    candidate.revision += 1
    session.execution = candidate
    return attempt


def complete_cleanup_attempt(
    session: CoreSession,
    *,
    cleanup_attempt_id: str,
    status: CleanupStatus,
    clock: RuntimeClock,
    detail: str = "",
    evidence_ids: tuple[str, ...] = (),
) -> CleanupOutcome:
    if status not in {
        CleanupStatus.SUCCESS,
        CleanupStatus.FAILED,
        CleanupStatus.UNCERTAIN,
        CleanupStatus.NOT_CONFIGURED,
    }:
        raise ValueError("cleanup completion requires a terminal cleanup status")
    attempt = session.execution.cleanup_attempts.get(cleanup_attempt_id)
    if attempt is None:
        raise ValueError("unknown cleanup attempt")
    if attempt.status is not CleanupAttemptStatus.STARTED:
        raise ValueError("cleanup attempt is already terminal")
    completed_at = clock.now()
    attempt_status = {
        CleanupStatus.SUCCESS: CleanupAttemptStatus.SUCCEEDED,
        CleanupStatus.FAILED: CleanupAttemptStatus.FAILED,
        CleanupStatus.UNCERTAIN: CleanupAttemptStatus.UNCERTAIN,
        CleanupStatus.NOT_CONFIGURED: CleanupAttemptStatus.NOT_CONFIGURED,
    }[status]
    terminal_attempt = attempt.model_copy(
        update={"status": attempt_status, "completed_at": completed_at}
    )
    terminal_attempt = CleanupAttempt.model_validate(terminal_attempt.model_dump(mode="python"))
    outcome = CleanupOutcome(
        cleanup_outcome_id=stable_id(
            "cleanup_out",
            attempt.cleanup_requirement_id,
            attempt.cleanup_attempt_id,
            status.value,
            detail,
            canonical_json(sorted(set(evidence_ids))),
        ),
        cleanup_requirement_id=attempt.cleanup_requirement_id,
        cleanup_attempt_id=attempt.cleanup_attempt_id,
        status=status,
        detail=detail,
        evidence_ids=tuple(sorted(set(evidence_ids))),
        completed_at=completed_at,
    )
    candidate = session.execution.model_copy(deep=True)
    candidate.cleanup_attempts[attempt.cleanup_attempt_id] = terminal_attempt
    candidate.cleanup_outcomes[outcome.cleanup_outcome_id] = outcome
    candidate.revision += 1
    session.execution = candidate
    return outcome


def finalize_unconfigured_cleanup(
    session: CoreSession,
    *,
    clock: RuntimeClock,
) -> tuple[CleanupOutcome, ...]:
    completed_requirements = {
        item.cleanup_requirement_id for item in session.execution.cleanup_outcomes.values()
    }
    outcomes: list[CleanupOutcome] = []
    for requirement_id in sorted(session.execution.cleanup_requirements):
        if requirement_id in completed_requirements:
            continue
        attempt = start_cleanup_attempt(
            session,
            cleanup_requirement_id=requirement_id,
            clock=clock,
        )
        outcomes.append(
            complete_cleanup_attempt(
                session,
                cleanup_attempt_id=attempt.cleanup_attempt_id,
                status=CleanupStatus.NOT_CONFIGURED,
                clock=clock,
                detail="cleanup executor not configured",
            )
        )
    return tuple(outcomes)
