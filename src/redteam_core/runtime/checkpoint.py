from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from pydantic import ConfigDict, Field, model_validator

from redteam_core.extension.bundle import ExtensionCatalog
from redteam_core.investigation.resolution import ResolverDescriptor
from redteam_core.knowledge.state import CoreSession
from redteam_core.models.base import SchemaModel, canonical_json, sha256_text, stable_id
from redteam_core.models.domain_state import DomainStateEnvelope
from redteam_core.models.execution import (
    CleanupAttempt,
    CleanupAttemptStatus,
    CleanupOutcome,
    CleanupStatus,
    ExperimentAttempt,
    ExperimentAttemptStatus,
)
from redteam_core.models.runtime import (
    RuntimeCompositionFingerprint,
    RuntimeLifecycle,
    RuntimeState,
)
from redteam_core.runtime.clock import RuntimeClock
from redteam_core.runtime.domain_state import validate_domain_state_envelopes
from redteam_core.runtime.journal import EventJournal, RuntimeEvent, RuntimeEventType

CHECKPOINT_SCHEMA_VERSION = "1.0"
CORE_RUNTIME_VERSION = "0.1.0"
RESOLVER_ROUTING_POLICY_ID = "core.resolver-routing.trust-desc-cost-asc-id-asc@1"


def core_state_schema_hash() -> str:
    payload = {
        "core_session": CoreSession.model_json_schema(),
        "domain_state_envelope": DomainStateEnvelope.model_json_schema(),
        "runtime_state": RuntimeState.model_json_schema(),
    }
    return "sha256:" + sha256_text(canonical_json(payload))


def build_runtime_composition_fingerprint(
    *,
    catalog: ExtensionCatalog,
    resolver_descriptors: tuple[ResolverDescriptor, ...] = (),
    priority_provider_ids: tuple[str, ...] = (),
) -> RuntimeCompositionFingerprint:
    rows: list[dict[str, object]] = []
    for descriptor in resolver_descriptors:
        dumped = descriptor.model_dump(mode="python")
        rows.append(dumped)
    rows.sort(key=lambda item: str(item["resolver_id"]))
    resolver_hash = "sha256:" + sha256_text(canonical_json(rows))
    providers = tuple(sorted(set(priority_provider_ids)))
    payload = {
        "semantic_manifest_hash": catalog.semantic_manifest_hash,
        "resolver_routing_hash": resolver_hash,
        "routing_policy_id": RESOLVER_ROUTING_POLICY_ID,
        "priority_provider_ids": list(providers),
    }
    return RuntimeCompositionFingerprint(
        composition_hash="sha256:" + sha256_text(canonical_json(payload)),
        semantic_manifest_hash=catalog.semantic_manifest_hash,
        resolver_routing_hash=resolver_hash,
        routing_policy_id=RESOLVER_ROUTING_POLICY_ID,
        priority_provider_ids=providers,
    )


def compatibility_hash(composition_fingerprint: RuntimeCompositionFingerprint) -> str:
    payload = {
        "checkpoint_schema_version": CHECKPOINT_SCHEMA_VERSION,
        "core_runtime_version": CORE_RUNTIME_VERSION,
        "core_state_schema_hash": core_state_schema_hash(),
        "runtime_composition_hash": composition_fingerprint.composition_hash,
    }
    return "sha256:" + sha256_text(canonical_json(payload))


class CheckpointEnvelope(SchemaModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True, frozen=True)

    schema_version: str = CHECKPOINT_SCHEMA_VERSION
    core_runtime_version: str = CORE_RUNTIME_VERSION
    checkpoint_sequence: int = Field(ge=1)
    checkpoint_id: str = Field(pattern=r"^checkpoint_[0-9a-f]{16}$")
    run_id: str = Field(min_length=1, max_length=200)
    reason: str = Field(min_length=1, max_length=500)
    evaluation_time: datetime
    composition_fingerprint: RuntimeCompositionFingerprint
    core_state_schema_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    compatibility_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    journal_sequence: int = Field(ge=0)
    journal_event_hash: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    state_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    session: CoreSession
    runtime_state: RuntimeState
    domain_states: tuple[DomainStateEnvelope, ...] = ()

    def _state_payload(self) -> dict[str, object]:
        return {
            "session": self.session.model_dump(mode="python"),
            "runtime_state": self.runtime_state.model_dump(mode="python"),
            "domain_states": [
                item.model_dump(mode="python")
                for item in sorted(self.domain_states, key=lambda value: value.bundle_id)
            ],
        }

    @model_validator(mode="after")
    def validate_envelope(self) -> CheckpointEnvelope:
        if self.schema_version != CHECKPOINT_SCHEMA_VERSION:
            raise ValueError("unsupported checkpoint schema version")
        if self.core_runtime_version != CORE_RUNTIME_VERSION:
            raise ValueError("unsupported core runtime version")
        if self.run_id != self.runtime_state.run_id:
            raise ValueError("checkpoint run_id does not match runtime state")
        if self.evaluation_time.tzinfo is None or self.evaluation_time.utcoffset() is None:
            raise ValueError("checkpoint evaluation_time must be timezone-aware")
        if self.journal_sequence == 0 and self.journal_event_hash is not None:
            raise ValueError("empty journal checkpoint cannot carry a journal hash")
        if self.journal_sequence > 0 and self.journal_event_hash is None:
            raise ValueError("non-empty journal checkpoint requires a journal hash")
        if len({item.bundle_id for item in self.domain_states}) != len(self.domain_states):
            raise ValueError("checkpoint may contain at most one domain state per bundle")
        if tuple(sorted(self.domain_states, key=lambda item: item.bundle_id)) != self.domain_states:
            raise ValueError("checkpoint domain states must be sorted by bundle_id")
        expected_state_hash = "sha256:" + sha256_text(canonical_json(self._state_payload()))
        if self.state_hash != expected_state_hash:
            raise ValueError("checkpoint state hash mismatch")
        expected_id = stable_id(
            "checkpoint",
            self.run_id,
            self.checkpoint_sequence,
            self.reason,
            self.compatibility_hash,
            self.state_hash,
            self.evaluation_time.isoformat(),
            self.journal_sequence,
            self.journal_event_hash or "",
        )
        if self.checkpoint_id != expected_id:
            raise ValueError("checkpoint_id does not match checkpoint identity")
        return self

    @classmethod
    def create(
        cls,
        *,
        checkpoint_sequence: int,
        run_id: str,
        reason: str,
        evaluation_time: datetime,
        semantic_manifest_hash: str,
        composition_fingerprint: RuntimeCompositionFingerprint,
        journal_sequence: int,
        journal_event_hash: str | None,
        session: CoreSession,
        runtime_state: RuntimeState,
        domain_states: tuple[DomainStateEnvelope, ...] = (),
    ) -> CheckpointEnvelope:
        schema_hash = core_state_schema_hash()
        if composition_fingerprint.semantic_manifest_hash != semantic_manifest_hash:
            raise ValueError("composition fingerprint does not match semantic manifest")
        compat_hash = compatibility_hash(composition_fingerprint)
        state_payload = {
            "session": session.model_dump(mode="python"),
            "runtime_state": runtime_state.model_dump(mode="python"),
            "domain_states": [item.model_dump(mode="python") for item in domain_states],
        }
        state_hash = "sha256:" + sha256_text(canonical_json(state_payload))
        checkpoint_id = stable_id(
            "checkpoint",
            run_id,
            checkpoint_sequence,
            reason,
            compat_hash,
            state_hash,
            evaluation_time.isoformat(),
            journal_sequence,
            journal_event_hash or "",
        )
        return cls(
            checkpoint_sequence=checkpoint_sequence,
            checkpoint_id=checkpoint_id,
            run_id=run_id,
            reason=reason,
            evaluation_time=evaluation_time,
            composition_fingerprint=composition_fingerprint,
            core_state_schema_hash=schema_hash,
            compatibility_hash=compat_hash,
            journal_sequence=journal_sequence,
            journal_event_hash=journal_event_hash,
            state_hash=state_hash,
            session=session,
            runtime_state=runtime_state,
            domain_states=domain_states,
        )


class AtomicCheckpointStore:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def _atomic_write(self, content: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                newline="\n",
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                delete=False,
            ) as stream:
                temporary_path = Path(stream.name)
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_path, self.path)
        finally:
            if temporary_path is not None and temporary_path.exists():
                temporary_path.unlink(missing_ok=True)

    def write(
        self,
        *,
        session: CoreSession,
        runtime_state: RuntimeState,
        catalog: ExtensionCatalog,
        composition_fingerprint: RuntimeCompositionFingerprint,
        journal: EventJournal,
        clock: RuntimeClock,
        reason: str,
        domain_states: tuple[DomainStateEnvelope, ...] = (),
    ) -> CheckpointEnvelope:
        if not catalog.frozen:
            raise RuntimeError("catalog must be frozen before checkpointing")
        if runtime_state.run_id != journal.run_id:
            raise ValueError("runtime state and journal run_id differ")
        if composition_fingerprint.semantic_manifest_hash != catalog.semantic_manifest_hash:
            raise ValueError("runtime composition fingerprint is stale for current catalog")
        previous_sequence = 0
        if self.path.exists():
            previous = self.load(
                catalog=catalog,
                composition_fingerprint=composition_fingerprint,
                journal=journal,
                expected_run_id=runtime_state.run_id,
            )
            previous_sequence = previous.checkpoint_sequence
        canonical_session = CoreSession.model_validate(session.model_dump(mode="python"))
        canonical_runtime = RuntimeState.model_validate(runtime_state.model_dump(mode="python"))
        canonical_domain_states = validate_domain_state_envelopes(catalog, domain_states)
        envelope = CheckpointEnvelope.create(
            checkpoint_sequence=previous_sequence + 1,
            run_id=runtime_state.run_id,
            reason=reason,
            evaluation_time=clock.now(),
            semantic_manifest_hash=catalog.semantic_manifest_hash,
            composition_fingerprint=composition_fingerprint,
            journal_sequence=journal.last_sequence,
            journal_event_hash=journal.head_hash,
            session=canonical_session,
            runtime_state=canonical_runtime,
            domain_states=canonical_domain_states,
        )
        self._atomic_write(canonical_json(envelope.model_dump(mode="json")))
        journal.append(
            RuntimeEventType.CHECKPOINT_WRITTEN,
            attributes={
                "checkpoint_id": envelope.checkpoint_id,
                "checkpoint_sequence": str(envelope.checkpoint_sequence),
                "reason": reason,
            },
        )
        return envelope

    def load(
        self,
        *,
        catalog: ExtensionCatalog,
        composition_fingerprint: RuntimeCompositionFingerprint,
        journal: EventJournal,
        expected_run_id: str | None = None,
    ) -> CheckpointEnvelope:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise ValueError(f"checkpoint not found: {self.path}") from exc
        try:
            envelope = CheckpointEnvelope.model_validate_json(raw)
        except Exception as exc:
            raise ValueError("checkpoint is invalid or corrupt") from exc
        if expected_run_id is not None and envelope.run_id != expected_run_id:
            raise ValueError("checkpoint belongs to a different run_id")
        if envelope.run_id != journal.run_id:
            raise ValueError("checkpoint and journal run_id differ")
        if (
            envelope.composition_fingerprint.semantic_manifest_hash
            != catalog.semantic_manifest_hash
        ):
            raise ValueError("checkpoint semantic manifest fingerprint mismatch")
        if envelope.composition_fingerprint != composition_fingerprint:
            raise ValueError("checkpoint runtime composition fingerprint mismatch")
        if envelope.core_state_schema_hash != core_state_schema_hash():
            raise ValueError("checkpoint core state schema fingerprint mismatch")
        if envelope.compatibility_hash != compatibility_hash(composition_fingerprint):
            raise ValueError("checkpoint runtime compatibility fingerprint mismatch")
        validate_domain_state_envelopes(catalog, envelope.domain_states)
        if journal.last_sequence < envelope.journal_sequence:
            raise ValueError("runtime journal is older than the checkpoint")
        if envelope.journal_sequence:
            bound = journal.event_at(envelope.journal_sequence)
            if bound is None or bound.event_hash != envelope.journal_event_hash:
                raise ValueError("checkpoint journal binding mismatch")
        return envelope


@dataclass(frozen=True)
class ResumeResult:
    checkpoint: CheckpointEnvelope
    session: CoreSession
    runtime_state: RuntimeState
    domain_states: tuple[DomainStateEnvelope, ...]
    requires_finalization: bool
    recovered_attempt_ids: tuple[str, ...]
    recovered_cleanup_attempt_ids: tuple[str, ...]


def _recover_experiment_attempt(
    attempt: ExperimentAttempt,
    *,
    event: RuntimeEvent | None,
    evaluation_time: datetime,
) -> ExperimentAttempt:
    if attempt.status.terminal:
        return attempt
    if attempt.status is ExperimentAttemptStatus.PLANNED:
        raise ValueError("journal claims an external effect for a PLANNED attempt")
    started_at = attempt.started_at or (event.timestamp if event is not None else evaluation_time)
    candidate = attempt.model_copy(
        update={
            "status": ExperimentAttemptStatus.UNCERTAIN,
            "started_at": started_at,
            "completed_at": evaluation_time,
            "failure_reason_code": "RESUME_AFTER_EFFECT_START",
        }
    )
    return ExperimentAttempt.model_validate(candidate.model_dump(mode="python"))


def _uncertain_cleanup_outcome(
    attempt: CleanupAttempt,
    *,
    completed_at: datetime,
) -> tuple[CleanupAttempt, CleanupOutcome]:
    terminal_attempt = attempt.model_copy(
        update={
            "status": CleanupAttemptStatus.UNCERTAIN,
            "completed_at": completed_at,
        }
    )
    terminal_attempt = CleanupAttempt.model_validate(
        terminal_attempt.model_dump(mode="python")
    )
    detail = "cleanup effect may have started before runtime interruption"
    outcome = CleanupOutcome(
        cleanup_outcome_id=stable_id(
            "cleanup_out",
            attempt.cleanup_requirement_id,
            attempt.cleanup_attempt_id,
            CleanupStatus.UNCERTAIN.value,
            detail,
            canonical_json([]),
        ),
        cleanup_requirement_id=attempt.cleanup_requirement_id,
        cleanup_attempt_id=attempt.cleanup_attempt_id,
        status=CleanupStatus.UNCERTAIN,
        detail=detail,
        completed_at=completed_at,
    )
    return terminal_attempt, outcome


def resume_checkpoint(
    store: AtomicCheckpointStore,
    *,
    catalog: ExtensionCatalog,
    composition_fingerprint: RuntimeCompositionFingerprint,
    journal: EventJournal,
    clock: RuntimeClock,
    expected_run_id: str | None = None,
) -> ResumeResult:
    envelope = store.load(
        catalog=catalog,
        composition_fingerprint=composition_fingerprint,
        journal=journal,
        expected_run_id=expected_run_id,
    )
    if envelope.runtime_state.lifecycle is RuntimeLifecycle.FINALIZED:
        raise ValueError("finalized runtime cannot be resumed")
    session = envelope.session.model_copy(deep=True)
    runtime_state = envelope.runtime_state.model_copy(deep=True)
    evaluation_time = clock.now()
    post_events = journal.events_after(envelope.journal_sequence)

    experiment_start_events = {
        event.attribute("attempt_id"): event
        for event in post_events
        if event.event_type is RuntimeEventType.EXPERIMENT_EFFECT_STARTED
    }
    execution = session.execution.model_copy(deep=True)
    recovered_attempts: list[str] = []
    for attempt_id, attempt in sorted(execution.attempts.items()):
        event = experiment_start_events.get(attempt_id)
        if attempt.status is ExperimentAttemptStatus.STARTED or event is not None:
            recovered = _recover_experiment_attempt(
                attempt,
                event=event,
                evaluation_time=evaluation_time,
            )
            if recovered != attempt:
                execution.attempts[attempt_id] = recovered
                recovered_attempts.append(attempt_id)
    unknown_effect_attempts = set(experiment_start_events) - set(execution.attempts)
    if unknown_effect_attempts:
        raise ValueError("journal references unknown experiment attempts")

    completed_cleanup_requirements = {
        item.cleanup_requirement_id for item in execution.cleanup_outcomes.values()
    }
    cleanup_start_events = [
        event
        for event in post_events
        if event.event_type is RuntimeEventType.CLEANUP_EFFECT_STARTED
    ]
    recovered_cleanup: list[str] = []
    for event in cleanup_start_events:
        cleanup_attempt_id = event.attribute("cleanup_attempt_id")
        requirement_id = event.attribute("cleanup_requirement_id")
        if requirement_id not in execution.cleanup_requirements:
            raise ValueError("journal references unknown cleanup requirement")
        if requirement_id in completed_cleanup_requirements:
            continue
        attempt = execution.cleanup_attempts.get(cleanup_attempt_id)
        if attempt is None:
            try:
                sequence = int(event.attribute("cleanup_sequence"))
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError("cleanup-start event lacks a valid sequence") from exc
            attempt = CleanupAttempt(
                cleanup_attempt_id=cleanup_attempt_id,
                sequence=sequence,
                cleanup_requirement_id=requirement_id,
                status=CleanupAttemptStatus.STARTED,
                started_at=event.timestamp,
            )
            execution.cleanup_attempt_sequence = max(
                execution.cleanup_attempt_sequence,
                sequence,
            )
        if attempt.status is CleanupAttemptStatus.STARTED:
            terminal, outcome = _uncertain_cleanup_outcome(
                attempt,
                completed_at=evaluation_time,
            )
            execution.cleanup_attempts[attempt.cleanup_attempt_id] = terminal
            execution.cleanup_outcomes[outcome.cleanup_outcome_id] = outcome
            completed_cleanup_requirements.add(requirement_id)
            recovered_cleanup.append(attempt.cleanup_attempt_id)
    for attempt_id, attempt in sorted(tuple(execution.cleanup_attempts.items())):
        if attempt.status is not CleanupAttemptStatus.STARTED:
            continue
        if attempt.cleanup_requirement_id in completed_cleanup_requirements:
            continue
        terminal, outcome = _uncertain_cleanup_outcome(
            attempt,
            completed_at=evaluation_time,
        )
        execution.cleanup_attempts[attempt_id] = terminal
        execution.cleanup_outcomes[outcome.cleanup_outcome_id] = outcome
        completed_cleanup_requirements.add(attempt.cleanup_requirement_id)
        recovered_cleanup.append(attempt_id)

    if recovered_attempts or recovered_cleanup:
        execution.revision += 1
        session.execution = type(session.execution).model_validate(
            execution.model_dump(mode="python")
        )

    notes = list(runtime_state.recovery_notes)
    if recovered_attempts:
        notes.append(f"uncertain_experiments:{','.join(sorted(recovered_attempts))}")
    if recovered_cleanup:
        notes.append(f"uncertain_cleanup:{','.join(sorted(set(recovered_cleanup)))}")
    runtime_state.resume_count += 1
    runtime_state.last_evaluation_time = evaluation_time
    runtime_state.recovery_notes = tuple(notes[-256:])
    runtime_state.revision += 1
    runtime_state = RuntimeState.model_validate(runtime_state.model_dump(mode="python"))

    journal.append(
        RuntimeEventType.RESUME_STARTED,
        attributes={
            "checkpoint_id": envelope.checkpoint_id,
            "recovered_experiments": str(len(set(recovered_attempts))),
            "recovered_cleanup": str(len(set(recovered_cleanup))),
        },
    )
    return ResumeResult(
        checkpoint=envelope,
        session=session,
        runtime_state=runtime_state,
        domain_states=envelope.domain_states,
        requires_finalization=runtime_state.lifecycle is RuntimeLifecycle.FINALIZING,
        recovered_attempt_ids=tuple(sorted(set(recovered_attempts))),
        recovered_cleanup_attempt_ids=tuple(sorted(set(recovered_cleanup))),
    )
