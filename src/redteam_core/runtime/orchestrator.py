from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from redteam_core.extension.bundle import ExtensionCatalog
from redteam_core.knowledge.state import CoreSession
from redteam_core.models.domain_state import DomainStateEnvelope
from redteam_core.models.runtime import (
    RuntimeBudgetLimits,
    RuntimeCompositionFingerprint,
    RuntimeState,
    RuntimeUsage,
    TerminationDecision,
)
from redteam_core.runtime.budget import record_runtime_usage
from redteam_core.runtime.checkpoint import (
    AtomicCheckpointStore,
    CheckpointEnvelope,
    ResumeResult,
    resume_checkpoint,
)
from redteam_core.runtime.clock import RuntimeClock
from redteam_core.runtime.finalizer import CleanupHandler, FinalizationResult, finalize_run
from redteam_core.runtime.journal import (
    EventJournal,
    JournalExperimentEffectObserver,
    RuntimeEventType,
)
from redteam_core.runtime.lease import RunLease
from redteam_core.runtime.termination import decide_termination


@dataclass
class RuntimeOrchestrator:
    catalog: ExtensionCatalog
    composition_fingerprint: RuntimeCompositionFingerprint
    clock: RuntimeClock
    journal: EventJournal
    checkpoint_store: AtomicCheckpointStore
    budget_limits: RuntimeBudgetLimits
    run_lease: RunLease | None = None
    _domain_states: tuple[DomainStateEnvelope, ...] | None = field(
        default=None, init=False, repr=False
    )

    def __post_init__(self) -> None:
        if self.run_lease is None:
            self.run_lease = RunLease(
                self.checkpoint_store.path.with_name("runtime.lock"),
                run_id=self.journal.run_id,
            )
        elif self.run_lease.run_id != self.journal.run_id:
            raise ValueError("runtime lease run_id differs from journal run_id")

    def _acquire_lease(self) -> None:
        assert self.run_lease is not None
        self.run_lease.acquire()

    def close(self) -> None:
        if self.run_lease is not None:
            self.run_lease.release()

    def _current_domain_states(self) -> tuple[DomainStateEnvelope, ...]:
        if self._domain_states is None:
            if self.checkpoint_store.path.exists():
                stored = self.checkpoint_store.load(
                    catalog=self.catalog,
                    composition_fingerprint=self.composition_fingerprint,
                    journal=self.journal,
                    expected_run_id=self.journal.run_id,
                )
                self._domain_states = stored.domain_states
            else:
                self._domain_states = ()
        return self._domain_states

    def start(self) -> RuntimeState:
        self._acquire_lease()
        if self.journal.events:
            self.close()
            raise ValueError("runtime journal is not empty; use resume instead of start")
        state = RuntimeState.create(self.journal.run_id)
        self.journal.append(RuntimeEventType.RUN_STARTED)
        return state

    def effect_observer(
        self,
        session: CoreSession,
        runtime_state: RuntimeState,
        *,
        domain_states: tuple[DomainStateEnvelope, ...] | None = None,
    ) -> DurableExperimentEffectObserver:
        self._acquire_lease()
        return DurableExperimentEffectObserver(
            session=session,
            runtime_state=runtime_state,
            catalog=self.catalog,
            composition_fingerprint=self.composition_fingerprint,
            journal=self.journal,
            checkpoint_store=self.checkpoint_store,
            clock=self.clock,
            domain_states=domain_states or (),
            checkpoint_writer=lambda reason: self.checkpoint(
                session,
                runtime_state,
                reason=reason,
                domain_states=domain_states,
            ),
        )

    def checkpoint(
        self,
        session: CoreSession,
        runtime_state: RuntimeState,
        *,
        reason: str,
        domain_states: tuple[DomainStateEnvelope, ...] | None = None,
    ) -> CheckpointEnvelope:
        self._acquire_lease()
        checkpoint = self.checkpoint_store.write(
            session=session,
            runtime_state=runtime_state,
            catalog=self.catalog,
            composition_fingerprint=self.composition_fingerprint,
            journal=self.journal,
            clock=self.clock,
            reason=reason,
            domain_states=self._current_domain_states() if domain_states is None else domain_states,
        )
        self._domain_states = checkpoint.domain_states
        return checkpoint

    def resume(self, *, expected_run_id: str | None = None) -> ResumeResult:
        self._acquire_lease()
        try:
            resumed = resume_checkpoint(
                self.checkpoint_store,
                catalog=self.catalog,
                composition_fingerprint=self.composition_fingerprint,
                journal=self.journal,
                clock=self.clock,
                expected_run_id=expected_run_id,
            )
            self._domain_states = resumed.domain_states
            return resumed
        except Exception:
            self.close()
            raise

    def record_usage(self, state: RuntimeState, usage: RuntimeUsage) -> RuntimeState:
        return record_runtime_usage(state, usage)

    def decide_termination(
        self,
        session: CoreSession,
        *,
        usage: RuntimeUsage,
        internal_error: str | None = None,
        handled_external_failure: str | None = None,
        normal_completion: bool = False,
        llm_finish_reason: str | None = None,
    ) -> TerminationDecision | None:
        return decide_termination(
            session,
            limits=self.budget_limits,
            usage=usage,
            evaluation_time=self.clock.now(),
            internal_error=internal_error,
            handled_external_failure=handled_external_failure,
            normal_completion=normal_completion,
            llm_finish_reason=llm_finish_reason,
        )

    async def finalize(
        self,
        session: CoreSession,
        runtime_state: RuntimeState,
        *,
        decision: TerminationDecision,
        cleanup_handler: CleanupHandler | None = None,
        domain_states: tuple[DomainStateEnvelope, ...] | None = None,
    ) -> FinalizationResult:
        self._acquire_lease()
        try:
            return await finalize_run(
                session,
                runtime_state,
                decision=decision,
                catalog=self.catalog,
                composition_fingerprint=self.composition_fingerprint,
                journal=self.journal,
                checkpoint_store=self.checkpoint_store,
                clock=self.clock,
                cleanup_handler=cleanup_handler,
                domain_states=self._current_domain_states()
                if domain_states is None
                else domain_states,
            )
        finally:
            self.close()


@dataclass
class DurableExperimentEffectObserver:
    """Durably checkpoints effect lifecycle around the external executor boundary."""

    session: CoreSession
    runtime_state: RuntimeState
    catalog: ExtensionCatalog
    composition_fingerprint: RuntimeCompositionFingerprint
    journal: EventJournal
    checkpoint_store: AtomicCheckpointStore
    clock: RuntimeClock
    domain_states: tuple[DomainStateEnvelope, ...] = ()
    checkpoint_writer: Callable[[str], CheckpointEnvelope] | None = None

    def _checkpoint(self, reason: str) -> None:
        if self.checkpoint_writer is not None:
            self.checkpoint_writer(reason)
        else:
            self.checkpoint_store.write(
                session=self.session,
                runtime_state=self.runtime_state,
                catalog=self.catalog,
                composition_fingerprint=self.composition_fingerprint,
                journal=self.journal,
                clock=self.clock,
                reason=reason,
                domain_states=self.domain_states,
            )

    def effect_started(self, *, attempt, descriptor) -> None:
        self._checkpoint(f"experiment_effect_started:{attempt.attempt_id}")
        JournalExperimentEffectObserver(self.journal).effect_started(
            attempt=attempt,
            descriptor=descriptor,
        )

    def effect_terminal(self, *, attempt, result) -> None:
        self._checkpoint(f"experiment_effect_terminal:{attempt.attempt_id}")
        JournalExperimentEffectObserver(self.journal).effect_terminal(
            attempt=attempt,
            result=result,
        )
