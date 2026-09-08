from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from pydantic import Field

from redteam_core.execution.cleanup import complete_cleanup_attempt, start_cleanup_attempt
from redteam_core.extension.bundle import ExtensionCatalog
from redteam_core.knowledge.state import CoreSession
from redteam_core.models.base import SchemaModel
from redteam_core.models.domain_state import DomainStateEnvelope
from redteam_core.models.execution import CleanupRequirement, CleanupStatus
from redteam_core.models.runtime import (
    FinalizationStatus,
    RunFinalization,
    RuntimeCompositionFingerprint,
    RuntimeLifecycle,
    RuntimeState,
    TerminationDecision,
)
from redteam_core.runtime.checkpoint import AtomicCheckpointStore, CheckpointEnvelope
from redteam_core.runtime.clock import RuntimeClock
from redteam_core.runtime.journal import EventJournal, RuntimeEventType


class CleanupCompletion(SchemaModel):
    status: CleanupStatus
    detail: str = Field(default="", max_length=1_000)
    evidence_ids: tuple[str, ...] = Field(default=(), max_length=128)

    def model_post_init(self, __context: object) -> None:
        if self.status not in {CleanupStatus.SUCCESS, CleanupStatus.FAILED}:
            raise ValueError("cleanup handler may only report SUCCESS or FAILED")


class CleanupHandler(Protocol):
    async def cleanup(self, requirement: CleanupRequirement) -> CleanupCompletion: ...


@dataclass(frozen=True)
class FinalizationResult:
    runtime_state: RuntimeState
    finalization: RunFinalization
    checkpoint: CheckpointEnvelope


async def finalize_run(
    session: CoreSession,
    runtime_state: RuntimeState,
    *,
    decision: TerminationDecision,
    catalog: ExtensionCatalog,
    composition_fingerprint: RuntimeCompositionFingerprint,
    journal: EventJournal,
    checkpoint_store: AtomicCheckpointStore,
    clock: RuntimeClock,
    cleanup_handler: CleanupHandler | None = None,
    domain_states: tuple[DomainStateEnvelope, ...] = (),
) -> FinalizationResult:
    if runtime_state.run_id != journal.run_id:
        raise ValueError("runtime state and journal run_id differ")
    if runtime_state.finalization is not None:
        checkpoint = checkpoint_store.write(
            session=session,
            runtime_state=runtime_state,
            catalog=catalog,
            composition_fingerprint=composition_fingerprint,
            journal=journal,
            clock=clock,
            reason="finalized_idempotent",
            domain_states=domain_states,
        )
        return FinalizationResult(
            runtime_state=runtime_state,
            finalization=runtime_state.finalization,
            checkpoint=checkpoint,
        )
    if runtime_state.termination is not None and runtime_state.termination != decision:
        raise ValueError("runtime already carries a different termination decision")

    started_at = clock.now()
    working_runtime = runtime_state.model_copy(deep=True)
    working_runtime.lifecycle = RuntimeLifecycle.FINALIZING
    working_runtime.termination = decision
    working_runtime.last_evaluation_time = started_at
    working_runtime.revision += 1
    working_runtime = RuntimeState.model_validate(working_runtime.model_dump(mode="python"))
    checkpoint_store.write(
        session=session,
        runtime_state=working_runtime,
        catalog=catalog,
        composition_fingerprint=composition_fingerprint,
        journal=journal,
        clock=clock,
        reason="finalization_started",
        domain_states=domain_states,
    )
    journal.append(
        RuntimeEventType.FINALIZATION_STARTED,
        attributes={
            "termination_decision_id": decision.decision_id,
            "termination_kind": decision.kind.value,
        },
    )

    completed_requirements = {
        outcome.cleanup_requirement_id for outcome in session.execution.cleanup_outcomes.values()
    }
    for requirement_id in sorted(session.execution.cleanup_requirements):
        if requirement_id in completed_requirements:
            continue
        requirement = session.execution.cleanup_requirements[requirement_id]
        attempt = start_cleanup_attempt(
            session,
            cleanup_requirement_id=requirement_id,
            clock=clock,
        )
        if cleanup_handler is None:
            outcome = complete_cleanup_attempt(
                session,
                cleanup_attempt_id=attempt.cleanup_attempt_id,
                status=CleanupStatus.NOT_CONFIGURED,
                clock=clock,
                detail="cleanup handler not configured",
            )
            checkpoint_store.write(
                session=session,
                runtime_state=working_runtime,
                catalog=catalog,
                composition_fingerprint=composition_fingerprint,
                journal=journal,
                clock=clock,
                reason=f"cleanup_terminal:{attempt.cleanup_attempt_id}",
                domain_states=domain_states,
            )
            journal.append(
                RuntimeEventType.CLEANUP_EFFECT_TERMINAL,
                attributes={
                    "cleanup_attempt_id": attempt.cleanup_attempt_id,
                    "cleanup_requirement_id": requirement_id,
                    "status": outcome.status.value,
                },
            )
            completed_requirements.add(requirement_id)
            continue

        try:
            checkpoint_store.write(
                session=session,
                runtime_state=working_runtime,
                catalog=catalog,
                composition_fingerprint=composition_fingerprint,
                journal=journal,
                clock=clock,
                reason=f"cleanup_effect_started:{attempt.cleanup_attempt_id}",
                domain_states=domain_states,
            )
            journal.append(
                RuntimeEventType.CLEANUP_EFFECT_STARTED,
                attributes={
                    "cleanup_attempt_id": attempt.cleanup_attempt_id,
                    "cleanup_requirement_id": requirement_id,
                    "cleanup_sequence": str(attempt.sequence),
                },
            )
        except Exception:
            complete_cleanup_attempt(
                session,
                cleanup_attempt_id=attempt.cleanup_attempt_id,
                status=CleanupStatus.FAILED,
                clock=clock,
                detail=(
                    "cleanup journal start marker could not be persisted; "
                    "effect was not invoked"
                ),
            )
            completed_requirements.add(requirement_id)
            raise

        try:
            completion = await cleanup_handler.cleanup(requirement)
            completion = CleanupCompletion.model_validate(completion.model_dump(mode="python"))
            outcome = complete_cleanup_attempt(
                session,
                cleanup_attempt_id=attempt.cleanup_attempt_id,
                status=completion.status,
                clock=clock,
                detail=completion.detail,
                evidence_ids=completion.evidence_ids,
            )
        except Exception as exc:
            outcome = complete_cleanup_attempt(
                session,
                cleanup_attempt_id=attempt.cleanup_attempt_id,
                status=CleanupStatus.UNCERTAIN,
                clock=clock,
                detail=f"cleanup handler raised {type(exc).__name__} after durable start marker",
            )
        checkpoint_store.write(
            session=session,
            runtime_state=working_runtime,
            catalog=catalog,
            composition_fingerprint=composition_fingerprint,
            journal=journal,
            clock=clock,
            reason=f"cleanup_terminal:{attempt.cleanup_attempt_id}",
            domain_states=domain_states,
        )
        journal.append(
            RuntimeEventType.CLEANUP_EFFECT_TERMINAL,
            attributes={
                "cleanup_attempt_id": attempt.cleanup_attempt_id,
                "cleanup_requirement_id": requirement_id,
                "status": outcome.status.value,
            },
        )
        completed_requirements.add(requirement_id)

    cleanup_outcomes = tuple(
        sorted(
            session.execution.cleanup_outcomes.values(),
            key=lambda item: item.cleanup_outcome_id,
        )
    )
    has_gap = any(outcome.status is not CleanupStatus.SUCCESS for outcome in cleanup_outcomes)
    finished_at = clock.now()
    finalization = RunFinalization.create(
        termination_decision_id=decision.decision_id,
        status=(
            FinalizationStatus.COMPLETE_WITH_CLEANUP_GAPS
            if has_gap
            else FinalizationStatus.COMPLETE
        ),
        started_at=started_at,
        finished_at=finished_at,
        cleanup_outcome_ids=tuple(item.cleanup_outcome_id for item in cleanup_outcomes),
    )
    working_runtime = RuntimeState.model_validate(
        working_runtime.model_copy(
            update={
                "lifecycle": RuntimeLifecycle.FINALIZED,
                "termination": decision,
                "finalization": finalization,
                "last_evaluation_time": finished_at,
                "revision": working_runtime.revision + 1,
            }
        ).model_dump(mode="python")
    )
    checkpoint = checkpoint_store.write(
        session=session,
        runtime_state=working_runtime,
        catalog=catalog,
        composition_fingerprint=composition_fingerprint,
        journal=journal,
        clock=clock,
        reason="finalized",
        domain_states=domain_states,
    )
    journal.append(
        RuntimeEventType.RUN_FINALIZED,
        attributes={
            "finalization_id": finalization.finalization_id,
            "status": finalization.status.value,
            "termination_decision_id": decision.decision_id,
        },
    )
    return FinalizationResult(
        runtime_state=working_runtime,
        finalization=finalization,
        checkpoint=checkpoint,
    )
