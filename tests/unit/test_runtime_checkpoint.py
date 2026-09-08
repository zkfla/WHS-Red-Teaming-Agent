from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

import pytest

from redteam_core.execution import (
    ExecutorCompletion,
    ExperimentExecutorDescriptor,
    ExperimentExecutorRegistry,
    PolicyConstraintDecision,
    PolicyConstraintEvaluatorDescriptor,
    PolicyConstraintEvaluatorRegistry,
    activate_execution_policy,
    authorize_attempt,
    compile_execution_policy,
    execute_authorized_attempt,
    plan_experiment,
    request_execution_stop,
)
from redteam_core.extension import (
    DomainBundle,
    DomainStateCodecRegistration,
    compose_bundles,
)
from redteam_core.investigation import ResolverDescriptor
from redteam_core.knowledge import CoreSession
from redteam_core.models import (
    CleanupRequirement,
    CleanupStatus,
    CreatedResource,
    EffectClass,
    ExperimentAttemptStatus,
    ExperimentDescriptor,
    FinalizationStatus,
    RuntimeBudgetLimits,
    RuntimeLifecycle,
    RuntimeState,
    RuntimeUsage,
    SchemaModel,
    TerminationDecision,
    TerminationKind,
    stable_id,
)
from redteam_core.runtime.budget import record_runtime_usage
from redteam_core.runtime.checkpoint import (
    AtomicCheckpointStore,
    build_runtime_composition_fingerprint,
    resume_checkpoint,
)
from redteam_core.runtime.domain_state import (
    DomainStateCodecDescriptor,
    DomainStateCodecRegistry,
    decode_domain_state,
    encode_domain_state,
)
from redteam_core.runtime.finalizer import CleanupCompletion, finalize_run
from redteam_core.runtime.journal import (
    EventJournal,
    JournalExperimentEffectObserver,
    RuntimeEventType,
)
from redteam_core.runtime.lease import RunLease
from redteam_core.runtime.orchestrator import RuntimeOrchestrator
from redteam_core.runtime.termination import decide_termination
from tests.helpers import FP_B, AlternatePayload, SamplePayload, catalog, reference_bundle

NOW = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)
ACTION_TYPE = "reference.experiment.value@1"
RESULT_TYPE = "reference.experiment_result.value@1"
CONSTRAINT_TYPE = "reference.policy.value@1"
EXECUTOR_ID = "reference.executor.value@1"
EVALUATOR_ID = "reference.policy_evaluator.value@1"
STATE_CODEC_ID = "reference.state_codec.value@1"


@dataclass
class MutableClock:
    value: datetime

    def now(self) -> datetime:
        return self.value


@dataclass
class AllowConstraintEvaluator:
    descriptor: PolicyConstraintEvaluatorDescriptor

    def evaluate(self, descriptor, constraint):
        return PolicyConstraintDecision(allowed=True, reason_code="ALLOW")


@dataclass
class ReferenceExecutor:
    descriptor: ExperimentExecutorDescriptor
    calls: list[str]

    async def execute(self, descriptor, permit):
        self.calls.append(permit.permit_id)
        payload = catalog().experiment_results.require(RESULT_TYPE).envelope_from_model(
            AlternatePayload(count=1)
        )
        return ExecutorCompletion(succeeded=True, result_payload=payload)


def _evaluators() -> PolicyConstraintEvaluatorRegistry:
    registry = PolicyConstraintEvaluatorRegistry()
    registry.register(
        AllowConstraintEvaluator(
            descriptor=PolicyConstraintEvaluatorDescriptor(
                evaluator_id=EVALUATOR_ID,
                constraint_type_ids=frozenset({CONSTRAINT_TYPE}),
            )
        )
    )
    registry.freeze()
    return registry


def _executors(calls: list[str]) -> ExperimentExecutorRegistry:
    registry = ExperimentExecutorRegistry()
    registry.register(
        ReferenceExecutor(
            descriptor=ExperimentExecutorDescriptor(
                executor_id=EXECUTOR_ID,
                action_type_ids=frozenset({ACTION_TYPE}),
                result_type_ids=frozenset({RESULT_TYPE}),
            ),
            calls=calls,
        )
    )
    registry.freeze()
    return registry


def _descriptor() -> ExperimentDescriptor:
    cat = catalog()
    payload = cat.experiment_actions.require(ACTION_TYPE).envelope_from_model(
        SamplePayload(value="execute")
    )
    return ExperimentDescriptor.create(
        action_type_id=ACTION_TYPE,
        subject_ref="subject:one",
        target_refs=("target:one",),
        effect_class=EffectClass.READ_ONLY,
        input_payload=payload,
    )


def _authorized_session(clock: MutableClock) -> tuple[CoreSession, ExperimentDescriptor, object]:
    cat = catalog()
    descriptor = _descriptor()
    constraint = cat.policy_constraints.require(CONSTRAINT_TYPE).envelope_from_model(
        SamplePayload(value="constraint")
    )
    policy = compile_execution_policy(
        catalog=cat,
        approval_ref="approval:runtime",
        approved_at=clock.now(),
        allowed_scope_refs=frozenset({descriptor.subject_ref, *descriptor.target_refs}),
        allowed_effect_classes=frozenset({descriptor.effect_class}),
        allowed_action_type_ids=frozenset({descriptor.action_type_id}),
        cleanup_required=False,
        constraints=(constraint,),
    )
    session = CoreSession()
    activate_execution_policy(session, policy=policy, catalog=cat, clock=clock)
    attempt = plan_experiment(session, descriptor=descriptor, catalog=cat, clock=clock)
    authorize_attempt(
        session,
        attempt_id=attempt.attempt_id,
        policy=policy,
        catalog=cat,
        evaluators=_evaluators(),
        clock=clock,
    )
    return session, descriptor, policy


def _changed_catalog():
    original = reference_bundle()
    changed = DomainBundle(
        manifest=original.manifest.model_copy(update={"implementation_fingerprint": FP_B}),
        schema_registrations=original.schema_registrations,
        relation_registrations=original.relation_registrations,
        requirement_registrations=original.requirement_registrations,
        candidate_deriver_registrations=original.candidate_deriver_registrations,
        validator_registrations=original.validator_registrations,
        experiment_executor_registrations=original.experiment_executor_registrations,
        policy_constraint_evaluator_registrations=(
            original.policy_constraint_evaluator_registrations
        ),
    )
    return compose_bundles((changed,))


def _composition(cat=None, *, resolver_descriptors=()):
    cat = cat or catalog()
    return build_runtime_composition_fingerprint(
        catalog=cat,
        resolver_descriptors=tuple(resolver_descriptors),
    )


class ReferenceDomainState(SchemaModel):
    value: str
    generation: int


@dataclass
class ReferenceDomainStateCodec:
    descriptor: DomainStateCodecDescriptor

    def encode(self, state: object) -> str:
        if not isinstance(state, ReferenceDomainState):
            raise TypeError("unexpected reference domain state")
        return json.dumps(state.model_dump(mode="json"), ensure_ascii=False)

    def validate(self, canonical_payload_json: str) -> None:
        ReferenceDomainState.model_validate_json(canonical_payload_json)

    def decode(self, canonical_payload_json: str) -> object:
        return ReferenceDomainState.model_validate_json(canonical_payload_json)


def _domain_state_catalog(*, fingerprint: str = "sha256:" + "a" * 64):
    bundle = reference_bundle()
    registration = DomainStateCodecRegistration(
        codec_id=STATE_CODEC_ID,
        owner_bundle_id="reference",
        state_schema_version="1",
        implementation_fingerprint=fingerprint,
    )
    return compose_bundles(
        (replace(bundle, domain_state_codec_registrations=(registration,)),)
    )


def _domain_state_registry(*, fingerprint: str = "sha256:" + "a" * 64):
    registry = DomainStateCodecRegistry()
    registry.register(
        ReferenceDomainStateCodec(
            descriptor=DomainStateCodecDescriptor(
                codec_id=STATE_CODEC_ID,
                owner_bundle_id="reference",
                state_schema_version="1",
                implementation_fingerprint=fingerprint,
            )
        )
    )
    registry.freeze()
    return registry


def test_event_journal_repairs_only_a_torn_final_json_line(tmp_path):
    clock = MutableClock(NOW)
    path = tmp_path / "events.jsonl"
    journal = EventJournal(path, run_id="run-one", clock=clock)
    first = journal.append(RuntimeEventType.RUN_STARTED)
    with path.open("ab") as stream:
        stream.write(b'{"schema_version":')
    restored = EventJournal(path, run_id="run-one", clock=clock)
    assert restored.events == (first,)
    assert path.read_text(encoding="utf-8").endswith("\n")


def test_event_journal_rejects_semantically_tampered_event_even_on_final_line(tmp_path):
    clock = MutableClock(NOW)
    path = tmp_path / "events.jsonl"
    journal = EventJournal(path, run_id="run-one", clock=clock)
    journal.append(RuntimeEventType.RUN_STARTED)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["attributes"] = {"tampered": "true"}
    path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="semantically invalid"):
        EventJournal(path, run_id="run-one", clock=clock)


def test_atomic_checkpoint_round_trip_and_no_temp_files(tmp_path):
    clock = MutableClock(NOW)
    journal = EventJournal(tmp_path / "events.jsonl", run_id="run-one", clock=clock)
    journal.append(RuntimeEventType.RUN_STARTED)
    store = AtomicCheckpointStore(tmp_path / "checkpoint.json")
    session = CoreSession()
    runtime_state = RuntimeState.create("run-one")
    written = store.write(
        session=session,
        runtime_state=runtime_state,
        catalog=(cat := catalog()),
        composition_fingerprint=_composition(cat),
        journal=journal,
        clock=clock,
        reason="round_complete",
    )
    restored = store.load(
        catalog=cat,
        composition_fingerprint=_composition(cat),
        journal=journal,
        expected_run_id="run-one",
    )
    assert restored == written
    assert restored.session == session
    assert not list(tmp_path.glob("*.tmp"))


def test_domain_state_codec_is_manifest_bound_and_checkpointed_opaquely(tmp_path):
    clock = MutableClock(NOW)
    cat = _domain_state_catalog()
    registry = _domain_state_registry()
    original_state = ReferenceDomainState(value="opaque", generation=7)
    envelope = encode_domain_state(
        bundle_id="reference",
        state=original_state,
        catalog=cat,
        registry=registry,
    )
    assert envelope.bundle_id == "reference"
    assert envelope.codec_id == STATE_CODEC_ID
    assert envelope.canonical_payload_json == '{"generation":7,"value":"opaque"}'

    journal = EventJournal(tmp_path / "events.jsonl", run_id="run-domain", clock=clock)
    journal.append(RuntimeEventType.RUN_STARTED)
    store = AtomicCheckpointStore(tmp_path / "checkpoint.json")
    composition = _composition(cat)
    written = store.write(
        session=CoreSession(),
        runtime_state=RuntimeState.create("run-domain"),
        catalog=cat,
        composition_fingerprint=composition,
        journal=journal,
        clock=clock,
        reason="domain-state",
        domain_states=(envelope,),
    )
    loaded = store.load(
        catalog=cat,
        composition_fingerprint=composition,
        journal=journal,
        expected_run_id="run-domain",
    )
    assert loaded.domain_states == written.domain_states == (envelope,)
    decoded = decode_domain_state(loaded.domain_states[0], catalog=cat, registry=registry)
    assert decoded == original_state
    resumed = resume_checkpoint(
        store,
        catalog=cat,
        composition_fingerprint=composition,
        journal=journal,
        clock=clock,
        expected_run_id="run-domain",
    )
    assert resumed.domain_states == (envelope,)
    assert resumed.requires_finalization is False


def test_domain_state_codec_semantics_participate_in_manifest_identity():
    first = _domain_state_catalog(fingerprint="sha256:" + "a" * 64)
    changed = _domain_state_catalog(fingerprint="sha256:" + "b" * 64)
    assert first.semantic_manifest_hash != changed.semantic_manifest_hash


def test_domain_state_runtime_registry_must_exactly_match_manifest():
    cat = _domain_state_catalog()
    empty = DomainStateCodecRegistry()
    empty.freeze()
    with pytest.raises(ValueError, match="codec set does not match semantic manifest"):
        encode_domain_state(
            bundle_id="reference",
            state=ReferenceDomainState(value="x", generation=1),
            catalog=cat,
            registry=empty,
        )


def test_checkpoint_rejects_unregistered_or_duplicate_domain_state(tmp_path):
    clock = MutableClock(NOW)
    cat = _domain_state_catalog()
    registry = _domain_state_registry()
    envelope = encode_domain_state(
        bundle_id="reference",
        state=ReferenceDomainState(value="x", generation=1),
        catalog=cat,
        registry=registry,
    )
    journal = EventJournal(tmp_path / "events.jsonl", run_id="run-domain", clock=clock)
    store = AtomicCheckpointStore(tmp_path / "checkpoint.json")
    composition = _composition(cat)
    with pytest.raises(ValueError, match="at most one domain state"):
        store.write(
            session=CoreSession(),
            runtime_state=RuntimeState.create("run-domain"),
            catalog=cat,
            composition_fingerprint=composition,
            journal=journal,
            clock=clock,
            reason="duplicate-domain-state",
            domain_states=(envelope, envelope),
        )

    plain = catalog()
    with pytest.raises(ValueError, match="unregistered codec"):
        AtomicCheckpointStore(tmp_path / "plain.json").write(
            session=CoreSession(),
            runtime_state=RuntimeState.create("run-domain"),
            catalog=plain,
            composition_fingerprint=_composition(plain),
            journal=journal,
            clock=clock,
            reason="foreign-domain-state",
            domain_states=(envelope,),
        )


def test_checkpoint_rejects_semantic_manifest_change(tmp_path):
    clock = MutableClock(NOW)
    journal = EventJournal(tmp_path / "events.jsonl", run_id="run-one", clock=clock)
    store = AtomicCheckpointStore(tmp_path / "checkpoint.json")
    cat = catalog()
    store.write(
        session=CoreSession(),
        runtime_state=RuntimeState.create("run-one"),
        catalog=cat,
        composition_fingerprint=_composition(cat),
        journal=journal,
        clock=clock,
        reason="safe",
    )
    changed = _changed_catalog()
    with pytest.raises(ValueError, match="semantic manifest fingerprint mismatch"):
        store.load(
            catalog=changed,
            composition_fingerprint=_composition(changed),
            journal=journal,
        )


def test_resume_recovers_post_checkpoint_effect_start_as_uncertain(tmp_path):
    clock = MutableClock(NOW)
    session, descriptor, _ = _authorized_session(clock)
    attempt = next(iter(session.execution.attempts.values()))
    journal = EventJournal(tmp_path / "events.jsonl", run_id="run-one", clock=clock)
    store = AtomicCheckpointStore(tmp_path / "checkpoint.json")
    cat = catalog()
    store.write(
        session=session,
        runtime_state=RuntimeState.create("run-one"),
        catalog=cat,
        composition_fingerprint=_composition(cat),
        journal=journal,
        clock=clock,
        reason="authorized",
    )
    clock.value += timedelta(seconds=1)
    journal.append(
        RuntimeEventType.EXPERIMENT_EFFECT_STARTED,
        attributes={
            "attempt_id": attempt.attempt_id,
            "attempt_sequence": str(attempt.sequence),
            "experiment_id": descriptor.experiment_id,
            "permit_id": attempt.permit_id or "",
        },
    )
    clock.value += timedelta(seconds=1)
    resumed = resume_checkpoint(
        store,
        catalog=cat,
        composition_fingerprint=_composition(cat),
        journal=journal,
        clock=clock,
    )
    recovered = resumed.session.execution.attempts[attempt.attempt_id]
    assert recovered.status is ExperimentAttemptStatus.UNCERTAIN
    assert recovered.failure_reason_code == "RESUME_AFTER_EFFECT_START"
    assert resumed.recovered_attempt_ids == (attempt.attempt_id,)


def test_resume_marks_checkpointed_started_attempt_uncertain_without_replaying(tmp_path):
    clock = MutableClock(NOW)
    session, descriptor, policy = _authorized_session(clock)
    calls: list[str] = []
    journal = EventJournal(tmp_path / "events.jsonl", run_id="run-one", clock=clock)
    observer = JournalExperimentEffectObserver(journal)
    # Simulate the durable start cut without invoking the executor: the journal marker is the
    # replay-prevention authority, not an assumed response from the target.
    attempt = next(iter(session.execution.attempts.values()))
    started = attempt.model_copy(
        update={"status": ExperimentAttemptStatus.STARTED, "started_at": clock.now()}
    )
    started = type(attempt).model_validate(started.model_dump(mode="python"))
    execution = session.execution.model_copy(deep=True)
    execution.attempts[attempt.attempt_id] = started
    execution.revision += 1
    session.execution = execution
    observer.effect_started(attempt=started, descriptor=descriptor)
    store = AtomicCheckpointStore(tmp_path / "checkpoint.json")
    cat = catalog()
    store.write(
        session=session,
        runtime_state=RuntimeState.create("run-one"),
        catalog=cat,
        composition_fingerprint=_composition(cat),
        journal=journal,
        clock=clock,
        reason="started",
    )
    resumed = resume_checkpoint(
        store,
        catalog=cat,
        composition_fingerprint=_composition(cat),
        journal=journal,
        clock=clock,
    )
    assert resumed.session.execution.attempts[attempt.attempt_id].status is (
        ExperimentAttemptStatus.UNCERTAIN
    )
    assert calls == []
    assert policy is not None


def test_resume_reconstructs_cleanup_start_after_checkpoint_as_uncertain(tmp_path):
    clock = MutableClock(NOW)
    session, _, _ = _authorized_session(clock)
    producing_attempt = next(iter(session.execution.attempts.values()))
    resource = CreatedResource(
        created_resource_id=stable_id(
            "created", "reference.resource.value@1", "resource:one", producing_attempt.attempt_id
        ),
        resource_id="resource:one",
        resource_type_id="reference.resource.value@1",
        created_by_attempt_id=producing_attempt.attempt_id,
    )
    requirement = CleanupRequirement.for_resource(resource)
    execution = session.execution.model_copy(deep=True)
    execution.created_resources[resource.created_resource_id] = resource
    execution.cleanup_requirements[requirement.cleanup_requirement_id] = requirement
    execution.revision += 1
    session.execution = execution
    journal = EventJournal(tmp_path / "events.jsonl", run_id="run-one", clock=clock)
    store = AtomicCheckpointStore(tmp_path / "checkpoint.json")
    cat = catalog()
    store.write(
        session=session,
        runtime_state=RuntimeState.create("run-one"),
        catalog=cat,
        composition_fingerprint=_composition(cat),
        journal=journal,
        clock=clock,
        reason="before_cleanup",
    )
    cleanup_attempt_id = stable_id("cleanup_attempt", requirement.cleanup_requirement_id, 1)
    journal.append(
        RuntimeEventType.CLEANUP_EFFECT_STARTED,
        attributes={
            "cleanup_attempt_id": cleanup_attempt_id,
            "cleanup_requirement_id": requirement.cleanup_requirement_id,
            "cleanup_sequence": "1",
        },
    )
    resumed = resume_checkpoint(
        store,
        catalog=cat,
        composition_fingerprint=_composition(cat),
        journal=journal,
        clock=clock,
    )
    assert resumed.recovered_cleanup_attempt_ids == (cleanup_attempt_id,)
    outcome = next(iter(resumed.session.execution.cleanup_outcomes.values()))
    assert outcome.status is CleanupStatus.UNCERTAIN


def test_termination_policy_and_budget_outrank_llm_finish():
    session = CoreSession()
    request_execution_stop(session, "policy says stop")
    decision = decide_termination(
        session,
        limits=RuntimeBudgetLimits(max_rounds=1),
        usage=RuntimeUsage(rounds=1),
        evaluation_time=NOW,
        llm_finish_reason="model_done",
    )
    assert decision is not None and decision.kind is TerminationKind.POLICY_STOP

    session = CoreSession()
    decision = decide_termination(
        session,
        limits=RuntimeBudgetLimits(max_rounds=1),
        usage=RuntimeUsage(rounds=1),
        evaluation_time=NOW,
        llm_finish_reason="model_done",
    )
    assert decision is not None and decision.kind is TerminationKind.BUDGET_STOP


def test_runtime_usage_is_monotonic():
    state = RuntimeState.create("run-one")
    state = record_runtime_usage(state, RuntimeUsage(rounds=2, tokens=10, requests=3))
    with pytest.raises(ValueError, match="cannot move backwards"):
        record_runtime_usage(state, RuntimeUsage(rounds=1, tokens=10, requests=3))


@pytest.mark.asyncio
async def test_effect_observer_journals_start_and_terminal_around_executor(tmp_path):
    clock = MutableClock(NOW)
    session, _, policy = _authorized_session(clock)
    attempt = next(iter(session.execution.attempts.values()))
    journal = EventJournal(tmp_path / "events.jsonl", run_id="run-one", clock=clock)
    calls: list[str] = []
    terminal, result = await execute_authorized_attempt(
        session,
        attempt_id=attempt.attempt_id,
        policy=policy,
        catalog=catalog(),
        evaluators=_evaluators(),
        executors=_executors(calls),
        clock=clock,
        effect_observer=JournalExperimentEffectObserver(journal),
    )
    assert calls
    assert terminal.status is ExperimentAttemptStatus.SUCCEEDED
    assert result is not None
    assert [event.event_type for event in journal.events] == [
        RuntimeEventType.EXPERIMENT_EFFECT_STARTED,
        RuntimeEventType.EXPERIMENT_EFFECT_TERMINAL,
    ]


@dataclass
class RaisingCleanupHandler:
    async def cleanup(self, requirement):
        raise TimeoutError("cleanup completion unknown")


@pytest.mark.asyncio
async def test_finalizer_preserves_cleanup_uncertainty_and_writes_terminal_checkpoint(tmp_path):
    clock = MutableClock(NOW)
    session, _, _ = _authorized_session(clock)
    producing_attempt = next(iter(session.execution.attempts.values()))
    resource = CreatedResource(
        created_resource_id=stable_id(
            "created", "reference.resource.value@1", "resource:one", producing_attempt.attempt_id
        ),
        resource_id="resource:one",
        resource_type_id="reference.resource.value@1",
        created_by_attempt_id=producing_attempt.attempt_id,
    )
    requirement = CleanupRequirement.for_resource(resource)
    execution = session.execution.model_copy(deep=True)
    execution.created_resources[resource.created_resource_id] = resource
    execution.cleanup_requirements[requirement.cleanup_requirement_id] = requirement
    execution.revision += 1
    session.execution = execution
    journal = EventJournal(tmp_path / "events.jsonl", run_id="run-one", clock=clock)
    store = AtomicCheckpointStore(tmp_path / "checkpoint.json")
    decision = decide_termination(
        session,
        limits=RuntimeBudgetLimits(),
        usage=RuntimeUsage(),
        evaluation_time=NOW,
        normal_completion=True,
    )
    assert decision is not None
    result = await finalize_run(
        session,
        RuntimeState.create("run-one"),
        decision=decision,
        catalog=(cat := catalog()),
        composition_fingerprint=_composition(cat),
        journal=journal,
        checkpoint_store=store,
        clock=clock,
        cleanup_handler=RaisingCleanupHandler(),
    )
    assert result.runtime_state.lifecycle is RuntimeLifecycle.FINALIZED
    assert result.finalization.status is FinalizationStatus.COMPLETE_WITH_CLEANUP_GAPS
    assert next(iter(session.execution.cleanup_outcomes.values())).status is CleanupStatus.UNCERTAIN
    loaded = store.load(
        catalog=cat,
        composition_fingerprint=_composition(cat),
        journal=journal,
    )
    assert loaded.runtime_state.finalization == result.finalization
    with pytest.raises(ValueError, match="finalized runtime"):
        resume_checkpoint(
            store,
            catalog=cat,
            composition_fingerprint=_composition(cat),
            journal=journal,
            clock=clock,
        )


def test_runtime_composition_fingerprint_binds_resolver_routing_policy(tmp_path):
    cat = catalog()
    first_descriptor = ResolverDescriptor(
        resolver_id="reference.resolver.runtime@1",
        provider_id="reference.local",
        supported_requirement_type_ids=frozenset({"reference.requirement.value@1"}),
        trust_rank=80,
        cost_rank=20,
    )
    changed_descriptor = first_descriptor.model_copy(update={"trust_rank": 81})
    first = _composition(cat, resolver_descriptors=(first_descriptor,))
    changed = _composition(cat, resolver_descriptors=(changed_descriptor,))
    assert first.composition_hash != changed.composition_hash

    clock = MutableClock(NOW)
    journal = EventJournal(tmp_path / "events.jsonl", run_id="run-one", clock=clock)
    store = AtomicCheckpointStore(tmp_path / "checkpoint.json")
    store.write(
        session=CoreSession(),
        runtime_state=RuntimeState.create("run-one"),
        catalog=cat,
        composition_fingerprint=first,
        journal=journal,
        clock=clock,
        reason="routing-v1",
    )
    with pytest.raises(ValueError, match="runtime composition fingerprint mismatch"):
        store.load(
            catalog=cat,
            composition_fingerprint=changed,
            journal=journal,
        )


def test_corrupt_existing_checkpoint_cannot_be_silently_overwritten(tmp_path):
    cat = catalog()
    composition = _composition(cat)
    clock = MutableClock(NOW)
    journal = EventJournal(tmp_path / "events.jsonl", run_id="run-one", clock=clock)
    journal.append(RuntimeEventType.RUN_STARTED)
    store = AtomicCheckpointStore(tmp_path / "checkpoint.json")
    store.write(
        session=CoreSession(),
        runtime_state=RuntimeState.create("run-one"),
        catalog=cat,
        composition_fingerprint=composition,
        journal=journal,
        clock=clock,
        reason="first",
    )
    payload = json.loads(store.path.read_text(encoding="utf-8"))
    payload["state_hash"] = "sha256:" + "0" * 64
    store.path.write_text(json.dumps(payload), encoding="utf-8")
    before = store.path.read_text(encoding="utf-8")
    with pytest.raises(ValueError, match="invalid or corrupt"):
        store.write(
            session=CoreSession(),
            runtime_state=RuntimeState.create("run-one"),
            catalog=cat,
            composition_fingerprint=composition,
            journal=journal,
            clock=clock,
            reason="must-not-overwrite",
        )
    assert store.path.read_text(encoding="utf-8") == before


def test_checkpoint_rejects_journal_older_than_bound_sequence(tmp_path):
    cat = catalog()
    composition = _composition(cat)
    clock = MutableClock(NOW)
    journal_path = tmp_path / "events.jsonl"
    journal = EventJournal(journal_path, run_id="run-one", clock=clock)
    journal.append(RuntimeEventType.RUN_STARTED)
    store = AtomicCheckpointStore(tmp_path / "checkpoint.json")
    store.write(
        session=CoreSession(),
        runtime_state=RuntimeState.create("run-one"),
        catalog=cat,
        composition_fingerprint=composition,
        journal=journal,
        clock=clock,
        reason="bound-to-run-start",
    )
    journal_path.write_text("", encoding="utf-8")
    older = EventJournal(journal_path, run_id="run-one", clock=clock)
    with pytest.raises(ValueError, match="journal is older"):
        store.load(
            catalog=cat,
            composition_fingerprint=composition,
            journal=older,
        )


def test_run_lease_enforces_single_writer_and_releases_cleanly(tmp_path):
    path = tmp_path / "runtime.lock"
    first = RunLease(path, run_id="run-one")
    second = RunLease(path, run_id="run-one")
    first.acquire()
    try:
        with pytest.raises(RuntimeError, match="already leased"):
            second.acquire()
    finally:
        first.release()
    second.acquire()
    assert second.held
    second.release()
    assert not second.held


def _orchestrator(tmp_path, clock: MutableClock):
    cat = catalog()
    composition = _composition(cat)
    journal = EventJournal(tmp_path / "events.jsonl", run_id="run-one", clock=clock)
    return RuntimeOrchestrator(
        catalog=cat,
        composition_fingerprint=composition,
        clock=clock,
        journal=journal,
        checkpoint_store=AtomicCheckpointStore(tmp_path / "checkpoint.json"),
        budget_limits=RuntimeBudgetLimits(),
    )


def test_durable_effect_observer_checkpoints_started_attempt_without_prior_checkpoint(tmp_path):
    clock = MutableClock(NOW)
    session, descriptor, _ = _authorized_session(clock)
    attempt = next(iter(session.execution.attempts.values()))
    started = attempt.model_copy(
        update={"status": ExperimentAttemptStatus.STARTED, "started_at": clock.now()}
    )
    started = type(attempt).model_validate(started.model_dump(mode="python"))
    execution = session.execution.model_copy(deep=True)
    execution.attempts[attempt.attempt_id] = started
    execution.revision += 1
    session.execution = execution

    orchestrator = _orchestrator(tmp_path, clock)
    runtime_state = RuntimeState.create("run-one")
    orchestrator.effect_observer(session, runtime_state).effect_started(
        attempt=started,
        descriptor=descriptor,
    )
    resumed = orchestrator.resume(expected_run_id="run-one")
    assert resumed.recovered_attempt_ids == (attempt.attempt_id,)
    assert resumed.session.execution.attempts[attempt.attempt_id].status is (
        ExperimentAttemptStatus.UNCERTAIN
    )


@dataclass
class StartDurableTerminalCrashObserver:
    durable: object

    def effect_started(self, *, attempt, descriptor):
        self.durable.effect_started(attempt=attempt, descriptor=descriptor)

    def effect_terminal(self, *, attempt, result):
        raise RuntimeError("simulated crash before terminal checkpoint")


@pytest.mark.asyncio
async def test_crash_before_terminal_checkpoint_resumes_started_effect_as_uncertain(tmp_path):
    clock = MutableClock(NOW)
    session, _, policy = _authorized_session(clock)
    attempt = next(iter(session.execution.attempts.values()))
    orchestrator = _orchestrator(tmp_path, clock)
    runtime_state = RuntimeState.create("run-one")
    observer = StartDurableTerminalCrashObserver(
        orchestrator.effect_observer(session, runtime_state)
    )
    calls: list[str] = []
    with pytest.raises(RuntimeError, match="effect-terminal observer failed"):
        await execute_authorized_attempt(
            session,
            attempt_id=attempt.attempt_id,
            policy=policy,
            catalog=orchestrator.catalog,
            evaluators=_evaluators(),
            executors=_executors(calls),
            clock=clock,
            effect_observer=observer,
        )
    assert calls
    assert session.execution.attempts[attempt.attempt_id].status is (
        ExperimentAttemptStatus.SUCCEEDED
    )
    resumed = orchestrator.resume(expected_run_id="run-one")
    assert resumed.session.execution.attempts[attempt.attempt_id].status is (
        ExperimentAttemptStatus.UNCERTAIN
    )


@dataclass
class CheckpointInspectingCleanupHandler:
    store: AtomicCheckpointStore
    cat: object
    composition: object
    journal: EventJournal
    saw_finalizing_started: bool = False

    async def cleanup(self, requirement):
        checkpoint = self.store.load(
            catalog=self.cat,
            composition_fingerprint=self.composition,
            journal=self.journal,
        )
        self.saw_finalizing_started = bool(
            checkpoint.runtime_state.lifecycle is RuntimeLifecycle.FINALIZING
            and any(
                attempt.status.value == "STARTED"
                for attempt in checkpoint.session.execution.cleanup_attempts.values()
            )
        )
        return CleanupCompletion(status=CleanupStatus.SUCCESS, detail="clean")


@pytest.mark.asyncio
async def test_finalizer_checkpoints_finalizing_and_cleanup_started_before_callback(tmp_path):
    clock = MutableClock(NOW)
    session, _, _ = _authorized_session(clock)
    producing_attempt = next(iter(session.execution.attempts.values()))
    resource = CreatedResource(
        created_resource_id=stable_id(
            "created",
            "reference.resource.value@1",
            "resource:finalizer",
            producing_attempt.attempt_id,
        ),
        resource_id="resource:finalizer",
        resource_type_id="reference.resource.value@1",
        created_by_attempt_id=producing_attempt.attempt_id,
    )
    requirement = CleanupRequirement.for_resource(resource)
    execution = session.execution.model_copy(deep=True)
    execution.created_resources[resource.created_resource_id] = resource
    execution.cleanup_requirements[requirement.cleanup_requirement_id] = requirement
    execution.revision += 1
    session.execution = execution

    cat = catalog()
    composition = _composition(cat)
    journal = EventJournal(tmp_path / "events.jsonl", run_id="run-one", clock=clock)
    store = AtomicCheckpointStore(tmp_path / "checkpoint.json")
    handler = CheckpointInspectingCleanupHandler(store, cat, composition, journal)
    decision = TerminationDecision.create(
        kind=TerminationKind.NORMAL_COMPLETION,
        reason="normal",
        evaluation_time=NOW,
    )
    await finalize_run(
        session,
        RuntimeState.create("run-one"),
        decision=decision,
        catalog=cat,
        composition_fingerprint=composition,
        journal=journal,
        checkpoint_store=store,
        clock=clock,
        cleanup_handler=handler,
    )
    assert handler.saw_finalizing_started


def test_resume_of_finalizing_checkpoint_requires_finalizer_reentry(tmp_path):
    clock = MutableClock(NOW)
    cat = catalog()
    composition = _composition(cat)
    journal = EventJournal(tmp_path / "events.jsonl", run_id="run-one", clock=clock)
    store = AtomicCheckpointStore(tmp_path / "checkpoint.json")
    decision = TerminationDecision.create(
        kind=TerminationKind.INTERNAL_ERROR,
        reason="crash-during-finalizer",
        evaluation_time=NOW,
    )
    runtime_state = RuntimeState.create("run-one").model_copy(
        update={
            "lifecycle": RuntimeLifecycle.FINALIZING,
            "termination": decision,
            "last_evaluation_time": NOW,
            "revision": 1,
        }
    )
    runtime_state = RuntimeState.model_validate(runtime_state.model_dump(mode="python"))
    store.write(
        session=CoreSession(),
        runtime_state=runtime_state,
        catalog=cat,
        composition_fingerprint=composition,
        journal=journal,
        clock=clock,
        reason="finalization_started",
    )
    resumed = resume_checkpoint(
        store,
        catalog=cat,
        composition_fingerprint=composition,
        journal=journal,
        clock=clock,
    )
    assert resumed.requires_finalization is True
    assert resumed.runtime_state.termination == decision


@pytest.mark.asyncio
async def test_all_terminal_kinds_share_the_same_finalizer_path(tmp_path):
    kinds = tuple(TerminationKind)
    for index, kind in enumerate(kinds):
        clock = MutableClock(NOW + timedelta(seconds=index))
        run_id = f"run-{index}"
        cat = catalog()
        composition = _composition(cat)
        journal = EventJournal(tmp_path / f"events-{index}.jsonl", run_id=run_id, clock=clock)
        store = AtomicCheckpointStore(tmp_path / f"checkpoint-{index}.json")
        decision = TerminationDecision.create(
            kind=kind,
            reason=f"reason-{kind.value}",
            evaluation_time=clock.now(),
        )
        finalized = await finalize_run(
            CoreSession(),
            RuntimeState.create(run_id),
            decision=decision,
            catalog=cat,
            composition_fingerprint=composition,
            journal=journal,
            checkpoint_store=store,
            clock=clock,
        )
        assert finalized.runtime_state.lifecycle is RuntimeLifecycle.FINALIZED
        assert finalized.runtime_state.termination == decision
