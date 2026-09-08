import asyncio
import json
from datetime import UTC, datetime

import pytest

from redteam_core.investigation.coverage import reconcile_candidate_state
from redteam_core.models import (
    DecisionAction,
    DecisionDriverDescriptor,
    DecisionDriverReply,
    DecisionProposal,
    DecisionStepOutcome,
    EffectClass,
    ExperimentDescriptor,
    RuntimeBudgetLimits,
    WorkingHypothesisUpsert,
    WorkingMemoryPatch,
)
from redteam_core.models.operational_budget import OperationalBudgetLimits
from redteam_core.reasoning import apply_working_memory_patch, reconcile_working_memory
from redteam_core.runtime import FixedClock
from redteam_core.runtime.checkpoint import (
    AtomicCheckpointStore,
    build_runtime_composition_fingerprint,
)
from redteam_core.runtime.journal import EventJournal
from redteam_core.runtime.orchestrator import RuntimeOrchestrator
from redteam_core.runtime.passive_scheduler import PassiveResolutionScheduler
from redteam_core.runtime.resolver_dispatch import DurableResolverDispatcher
from redteam_core.runtime.step_loop import GenericStepEngine
from tests.helpers import SamplePayload, catalog
from tests.integration.test_durable_resolver_dispatch import FakeResolver, registry
from tests.unit.test_focused_context import fixture


class ScriptedDriver:
    descriptor = DecisionDriverDescriptor(
        driver_id="reference.fake-decision@1",
        provider_id="reference.fake-provider",
        tokenizer_id="reference.synthetic-utf8@1",
        max_input_tokens=8_192,
        max_output_tokens=512,
    )

    def __init__(self, payloads, *, fail=False):
        self.payloads = list(payloads)
        self.calls = 0
        self.feedback = []
        self.fail = fail

    async def propose(self, context, *, repair_feedback=None):
        self.calls += 1
        self.feedback.append(repair_feedback)
        if self.fail:
            raise RuntimeError("synthetic provider completion unknown")
        return DecisionDriverReply(
            payload=self.payloads.pop(0), input_tokens=100, output_tokens=50
        )


def proposal(session, action, **kwargs):
    return DecisionProposal.create(
        action=action,
        expected_knowledge_revision=session.knowledge.revision,
        expected_candidate_revision=session.candidate_state.revision,
        expected_working_memory_revision=session.working_memory.revision,
        expected_expectation_revision=session.expectations.revision,
        **kwargs,
    ).model_dump(mode="json")


def setup_engine(
    tmp_path,
    payloads,
    *,
    prepare=None,
    scope=None,
    fail=False,
    max_requests=20,
    runtime_max_requests=None,
    runtime_max_tokens=200_000,
    max_rounds=None,
    experiment_plan_factory=None,
):
    session, candidate, _req, _secret = fixture()
    reconcile_candidate_state(session, (candidate,))
    if prepare:
        prepare(session, candidate)
    cat = catalog()
    clock = FixedClock(datetime(2026, 9, 8, tzinfo=UTC))
    journal = EventJournal(tmp_path / "events.jsonl", run_id="step:one", clock=clock)
    driver = ScriptedDriver(payloads, fail=fail)
    composition = build_runtime_composition_fingerprint(
        catalog=cat, decision_driver_hash=driver.descriptor.descriptor_hash
    )
    orchestrator = RuntimeOrchestrator(
        cat,
        composition,
        clock,
        journal,
        AtomicCheckpointStore(tmp_path / "state.json"),
        RuntimeBudgetLimits(
            max_rounds=max_rounds,
            max_requests=runtime_max_requests or max_requests,
            max_tokens=runtime_max_tokens,
        ),
    )
    runtime = orchestrator.start()
    DurableResolverDispatcher.initialize(
        session=session,
        runtime=runtime,
        orchestrator=orchestrator,
        limits=OperationalBudgetLimits(max_requests=max_requests, max_tokens=200_000),
    )
    engine = GenericStepEngine(
        session=session,
        runtime=runtime,
        orchestrator=orchestrator,
        driver=driver,
        active_candidate_ids=(candidate.candidate_id,),
        scope_completion=scope,
        experiment_plans=(
            experiment_plan_factory(cat, candidate) if experiment_plan_factory else None
        ),
        count_tokens=lambda content: len(content.encode("utf-8")) // 4,
    )
    return engine, session, runtime, candidate, driver, orchestrator


@pytest.mark.asyncio
async def test_finish_is_only_a_request_and_incomplete_scope_is_partial(tmp_path):
    engine, session, runtime, _candidate, driver, orchestrator = setup_engine(
        tmp_path, []
    )
    driver.payloads.append(proposal(session, DecisionAction.FINISH, finish_reason="done"))
    result = await engine.run_step()
    assert result.outcome is DecisionStepOutcome.FINISH_REQUESTED
    assert result.completion.status.value == "PARTIAL"
    assert "ACQUISITION_SCOPE_INCOMPLETE" in result.completion.reasons
    assert runtime.progress.receipts[-1] == result.receipt
    assert runtime.completion_assessments[-1].assessment == result.completion
    assert runtime.completion_assessments[-1].step_index == result.receipt.step_index
    assert runtime.termination is None and runtime.lifecycle.value == "RUNNING"
    finalized = await orchestrator.finalize(
        session,
        runtime,
        decision=result.termination,
    )
    assert finalized.runtime_state.lifecycle.value == "FINALIZED"
    assert result.completion.status.value == "PARTIAL"


@pytest.mark.asyncio
async def test_wrong_revision_and_forged_evidence_are_rejected_with_one_repair(tmp_path):
    engine, session, runtime, _candidate, driver, orchestrator = setup_engine(
        tmp_path, [{"action": "PATCH_WORKING_MEMORY", "evidence_ids": ["forged"]}]
    )
    wrong = proposal(session, DecisionAction.FINISH, finish_reason="wrong revision")
    wrong["expected_knowledge_revision"] += 1
    # Re-hashing is deliberately unavailable to the driver; stale content remains invalid.
    driver.payloads.append(wrong)
    before = session.model_dump(mode="json")
    result = await engine.run_step()
    assert result.outcome is DecisionStepOutcome.REJECTED
    assert driver.calls == 2 and driver.feedback[0] is None
    assert driver.feedback[1] is not None
    assert session.model_dump(mode="json") == before
    assert len(result.receipt.request_attempt_ids) == 2
    assert runtime.usage.requests == 2
    orchestrator.close()


@pytest.mark.asyncio
async def test_malformed_repair_cannot_exceed_durable_request_budget(tmp_path):
    engine, _session, runtime, _candidate, driver, orchestrator = setup_engine(
        tmp_path,
        [{"not": "a proposal"}],
        max_requests=1,
    )
    result = await engine.run_step()
    assert result.outcome is DecisionStepOutcome.STOP_PARTIAL
    assert result.detail.startswith("operational request budget exhausted")
    assert driver.calls == runtime.usage.requests == 1
    assert len(result.receipt.request_attempt_ids) == 1
    orchestrator.close()


@pytest.mark.asyncio
async def test_context_token_projection_stops_before_driver_dispatch(tmp_path):
    engine, _session, runtime, _candidate, driver, orchestrator = setup_engine(tmp_path, [])
    engine.count_tokens = lambda _content: driver.descriptor.max_input_tokens + 1
    result = await engine.run_step()
    assert result.outcome is DecisionStepOutcome.STOP_PARTIAL
    assert driver.calls == runtime.usage.requests == 0
    assert result.receipt.request_attempt_ids == ()
    orchestrator.close()


@pytest.mark.asyncio
async def test_invalid_tokenizer_adapter_fails_closed(tmp_path):
    engine, _session, _runtime, _candidate, driver, orchestrator = setup_engine(tmp_path, [])
    engine.count_tokens = lambda _content: True
    with pytest.raises(ValueError, match="tokenizer adapter"):
        await engine.run_step()
    assert driver.calls == 0
    orchestrator.close()


@pytest.mark.asyncio
async def test_combined_runtime_request_budget_stops_with_operational_headroom(tmp_path):
    engine, _session, runtime, _candidate, driver, orchestrator = setup_engine(
        tmp_path,
        [{"not": "a proposal"}],
        max_requests=20,
        runtime_max_requests=1,
    )
    result = await engine.run_step()
    assert result.outcome is DecisionStepOutcome.STOP_PARTIAL
    assert result.detail.startswith("operational request budget exhausted")
    assert driver.calls == runtime.usage.requests == 1
    assert runtime.operational_budget.limits.max_requests == 20
    orchestrator.close()


@pytest.mark.asyncio
async def test_combined_runtime_token_budget_stops_before_dispatch(tmp_path):
    engine, _session, runtime, _candidate, driver, orchestrator = setup_engine(
        tmp_path,
        [],
        max_requests=20,
        runtime_max_tokens=1_000,
    )
    result = await engine.run_step()
    assert result.outcome is DecisionStepOutcome.STOP_PARTIAL
    assert driver.calls == runtime.usage.requests == 0
    assert "budget exhausted" in result.detail
    orchestrator.close()


@pytest.mark.asyncio
async def test_round_budget_counts_completed_steps_and_stops_before_next_dispatch(tmp_path):
    engine, session, runtime, candidate, driver, orchestrator = setup_engine(
        tmp_path,
        [],
        max_rounds=1,
    )
    query = proposal(
        session,
        DecisionAction.REQUEST_EXPERIMENT_PLAN,
        candidate_id=candidate.candidate_id,
    )
    driver.payloads.extend([query, query])
    first = await engine.run_step()
    second = await engine.run_step()
    assert first.outcome is DecisionStepOutcome.WAITING
    assert runtime.usage.rounds == 1
    assert second.outcome is DecisionStepOutcome.STOP_PARTIAL
    assert second.receipt is None
    assert driver.calls == 1
    orchestrator.close()


@pytest.mark.asyncio
async def test_semantic_noop_replans_once_then_stops_partial(tmp_path):
    no_op_payloads = []

    def prepare(session, candidate):
        created = apply_working_memory_patch(
            session,
            WorkingMemoryPatch(
                expected_revision=0,
                upserts=(
                    WorkingHypothesisUpsert(
                        semantic_key="stable",
                        candidate_id=candidate.candidate_id,
                        claim="Stable claim",
                        decision_summary="Stable summary",
                    ),
                ),
            ),
        )
        assert created.accepted and created.changed
        reconcile_working_memory(session)

    engine, session, _runtime, _candidate, driver, orchestrator = setup_engine(
        tmp_path, no_op_payloads, prepare=prepare
    )
    patch = WorkingMemoryPatch(
        expected_revision=session.working_memory.revision,
        focus_hypothesis_id=session.working_memory.focus_hypothesis_id,
    )
    item = proposal(session, DecisionAction.PATCH_WORKING_MEMORY, memory_patch=patch)
    driver.payloads.extend([item, item, item, item])
    outcomes = [(await engine.run_step()).outcome for _ in range(4)]
    assert outcomes == [
        DecisionStepOutcome.NO_PROGRESS,
        DecisionStepOutcome.NO_PROGRESS,
        DecisionStepOutcome.REPLAN_REQUIRED,
        DecisionStepOutcome.STOP_PARTIAL,
    ]
    orchestrator.close()


@pytest.mark.asyncio
async def test_sentence_edits_do_not_evade_semantic_no_progress_detection(tmp_path):
    def prepare(session, candidate):
        created = apply_working_memory_patch(
            session,
            WorkingMemoryPatch(
                expected_revision=0,
                upserts=(
                    WorkingHypothesisUpsert(
                        semantic_key="wording",
                        candidate_id=candidate.candidate_id,
                        claim="Immutable claim",
                        decision_summary="Initial wording",
                    ),
                ),
            ),
        )
        assert created.accepted
        reconcile_working_memory(session)

    engine, session, _runtime, _candidate, driver, orchestrator = setup_engine(
        tmp_path, [], prepare=prepare
    )
    hypothesis_id = session.working_memory.focus_hypothesis_id
    outcomes = []
    for index in range(3):
        patch = WorkingMemoryPatch(
            expected_revision=session.working_memory.revision,
            upserts=(
                WorkingHypothesisUpsert(
                    hypothesis_id=hypothesis_id,
                    decision_summary=f"Cosmetic wording {index}",
                ),
            ),
        )
        driver.payloads.append(
            proposal(session, DecisionAction.PATCH_WORKING_MEMORY, memory_patch=patch)
        )
        outcomes.append((await engine.run_step()).outcome)
    assert outcomes == [
        DecisionStepOutcome.NO_PROGRESS,
        DecisionStepOutcome.NO_PROGRESS,
        DecisionStepOutcome.REPLAN_REQUIRED,
    ]
    orchestrator.close()


@pytest.mark.asyncio
async def test_waiting_does_not_count_as_progress_but_polling_is_bounded(tmp_path):
    engine, session, _runtime, candidate, driver, orchestrator = setup_engine(tmp_path, [])
    item = proposal(
        session,
        DecisionAction.REQUEST_EXPERIMENT_PLAN,
        candidate_id=candidate.candidate_id,
    )
    driver.payloads.extend([item, item, item])
    outcomes = [(await engine.run_step()).outcome for _ in range(3)]
    assert outcomes == [
        DecisionStepOutcome.WAITING,
        DecisionStepOutcome.WAITING,
        DecisionStepOutcome.STOP_PARTIAL,
    ]
    orchestrator.close()


@pytest.mark.asyncio
async def test_query_context_persists_bounded_metadata_for_the_next_step(tmp_path):
    engine, session, runtime, candidate, driver, orchestrator = setup_engine(tmp_path, [])
    retrieval_ref = f"candidate:{candidate.candidate_id}:relations"
    driver.payloads.append(
        proposal(
            session,
            DecisionAction.QUERY_CONTEXT,
            candidate_id=candidate.candidate_id,
            retrieval_ref=retrieval_ref,
        )
    )
    result = await engine.run_step()
    assert result.outcome is DecisionStepOutcome.NO_PROGRESS
    assert result.detail == "CONTEXT_RETRIEVED"
    assert len(runtime.progress.context_retrievals) == 1
    retrieval = runtime.progress.context_retrievals[0]
    assert retrieval.retrieval_ref == retrieval_ref and retrieval.complete
    assert '"raw_payloads_included":false' in retrieval.content
    assert "token=must-never-enter-context" not in retrieval.content
    projected = json.loads(engine._context())["channels"]["CANONICAL_STATE"]
    assert json.loads(retrieval.content) in projected["retrieval_results"]
    orchestrator.close()


@pytest.mark.asyncio
async def test_experiment_request_can_only_plan_a_trusted_prebound_descriptor(tmp_path):
    def plans(cat, candidate):
        payload = cat.experiment_actions.require(
            "reference.experiment.value@1"
        ).envelope_from_model(SamplePayload(value="bounded-plan"))
        return {
            candidate.candidate_id: ExperimentDescriptor.create(
                action_type_id="reference.experiment.value@1",
                subject_ref="subject:bounded",
                target_refs=("target:bounded",),
                effect_class=EffectClass.READ_ONLY,
                input_payload=payload,
            )
        }

    engine, session, _runtime, candidate, driver, orchestrator = setup_engine(
        tmp_path, [], experiment_plan_factory=plans
    )
    driver.payloads.append(
        proposal(
            session,
            DecisionAction.REQUEST_EXPERIMENT_PLAN,
            candidate_id=candidate.candidate_id,
        )
    )
    result = await engine.run_step()
    assert result.outcome is DecisionStepOutcome.PROGRESSED
    assert result.detail.startswith("EXPERIMENT_PLANNED:attempt_")
    assert len(session.execution.experiments) == len(session.execution.attempts) == 1
    attempt = next(iter(session.execution.attempts.values()))
    assert attempt.status.value == "PLANNED"
    orchestrator.close()


@pytest.mark.asyncio
async def test_a_b_waiting_ping_pong_replans_once_then_stops(tmp_path):
    engine, session, _runtime, candidate, driver, orchestrator = setup_engine(tmp_path, [])
    requirement_id = candidate.requirement_ids[0]
    items = [
        proposal(
            session,
            DecisionAction.REQUEST_EXPERIMENT_PLAN,
            candidate_id=candidate.candidate_id,
        ),
        proposal(
            session,
            DecisionAction.RESOLVE_REQUIREMENT,
            requirement_id=requirement_id,
        ),
    ]
    driver.payloads.extend([items[0], items[1], items[0], items[1], items[0]])
    outcomes = [(await engine.run_step()).outcome for _ in range(5)]
    assert outcomes == [
        DecisionStepOutcome.WAITING,
        DecisionStepOutcome.WAITING,
        DecisionStepOutcome.WAITING,
        DecisionStepOutcome.REPLAN_REQUIRED,
        DecisionStepOutcome.STOP_PARTIAL,
    ]
    orchestrator.close()


def test_driver_descriptor_is_bound_to_runtime_composition(tmp_path):
    engine, session, runtime, candidate, _driver, orchestrator = setup_engine(tmp_path, [])
    replacement = ScriptedDriver([])
    replacement.descriptor = replacement.descriptor.model_copy(
        update={"driver_id": "reference.different-driver@1"}
    )
    with pytest.raises(RuntimeError, match="composition mismatch"):
        GenericStepEngine(
            session=session,
            runtime=runtime,
            orchestrator=orchestrator,
            driver=replacement,
            active_candidate_ids=(candidate.candidate_id,),
            count_tokens=lambda content: len(content.encode("utf-8")) // 4,
        )
    engine.orchestrator.close()


@pytest.mark.asyncio
async def test_uncertain_driver_call_is_not_replayed_after_resume(tmp_path):
    engine, _session, _runtime, _candidate, driver, orchestrator = setup_engine(
        tmp_path, [], fail=True
    )
    result = await engine.run_step()
    assert result.outcome is DecisionStepOutcome.WAITING and driver.calls == 1
    orchestrator.close()

    cat = catalog()
    clock = FixedClock(datetime(2026, 9, 8, tzinfo=UTC))
    journal = EventJournal(tmp_path / "events.jsonl", run_id="step:one", clock=clock)
    resumed_orchestrator = RuntimeOrchestrator(
        cat,
        build_runtime_composition_fingerprint(
            catalog=cat, decision_driver_hash=ScriptedDriver.descriptor.descriptor_hash
        ),
        clock,
        journal,
        AtomicCheckpointStore(tmp_path / "state.json"),
        RuntimeBudgetLimits(max_requests=20, max_tokens=200_000),
    )
    resumed = resumed_orchestrator.resume(expected_run_id="step:one")
    replacement = ScriptedDriver([])
    resumed_engine = GenericStepEngine(
        session=resumed.session,
        runtime=resumed.runtime_state,
        orchestrator=resumed_orchestrator,
        driver=replacement,
        active_candidate_ids=tuple(resumed.session.candidate_state.candidates),
        count_tokens=lambda content: len(content.encode("utf-8")) // 4,
    )
    waiting = await resumed_engine.run_step()
    assert waiting.outcome is DecisionStepOutcome.WAITING
    assert replacement.calls == 0
    resumed_orchestrator.close()


@pytest.mark.asyncio
async def test_recorded_response_resumes_validation_without_second_driver_call(
    tmp_path, monkeypatch
):
    engine, session, runtime, _candidate, driver, orchestrator = setup_engine(tmp_path, [])
    driver.payloads.append(proposal(session, DecisionAction.FINISH, finish_reason="resume"))

    def crash_after_response(_proposal):
        raise RuntimeError("synthetic crash after durable response")

    monkeypatch.setattr(engine, "_validate_proposal", crash_after_response)
    with pytest.raises(RuntimeError, match="after durable response"):
        await engine.run_step()
    assert driver.calls == 1 and len(runtime.decision_responses) == 1
    assert runtime.progress.active_attempt_id is None
    orchestrator.close()

    cat = catalog()
    clock = FixedClock(datetime(2026, 9, 8, tzinfo=UTC))
    replacement = ScriptedDriver([])
    resumed_orchestrator = RuntimeOrchestrator(
        cat,
        build_runtime_composition_fingerprint(
            catalog=cat, decision_driver_hash=replacement.descriptor.descriptor_hash
        ),
        clock,
        EventJournal(tmp_path / "events.jsonl", run_id="step:one", clock=clock),
        AtomicCheckpointStore(tmp_path / "state.json"),
        RuntimeBudgetLimits(max_requests=20, max_tokens=200_000),
    )
    resumed = resumed_orchestrator.resume(expected_run_id="step:one")
    changed_authority_engine = GenericStepEngine(
        session=resumed.session,
        runtime=resumed.runtime_state,
        orchestrator=resumed_orchestrator,
        driver=replacement,
        active_candidate_ids=tuple(resumed.session.candidate_state.candidates),
        authority_epoch="permissions-changed",
        count_tokens=lambda content: len(content.encode("utf-8")) // 4,
    )
    with pytest.raises(RuntimeError, match="no longer matches canonical input"):
        await changed_authority_engine.run_step()
    assert replacement.calls == 0
    result = await GenericStepEngine(
        session=resumed.session,
        runtime=resumed.runtime_state,
        orchestrator=resumed_orchestrator,
        driver=replacement,
        active_candidate_ids=tuple(resumed.session.candidate_state.candidates),
        count_tokens=lambda content: len(content.encode("utf-8")) // 4,
    ).run_step()
    assert result.outcome is DecisionStepOutcome.FINISH_REQUESTED
    assert replacement.calls == 0 and len(result.receipt.request_attempt_ids) == 1
    resumed_orchestrator.close()


@pytest.mark.asyncio
async def test_storage_failure_poisons_engine_before_dispatch(tmp_path, monkeypatch):
    engine, _session, _runtime, _candidate, driver, orchestrator = setup_engine(tmp_path, [])

    def fail_checkpoint(*_args, **_kwargs):
        raise OSError("synthetic checkpoint failure")

    monkeypatch.setattr(orchestrator, "checkpoint", fail_checkpoint)
    with pytest.raises(OSError, match="checkpoint failure"):
        await engine.run_step()
    assert driver.calls == 0
    with pytest.raises(RuntimeError, match="storage failed"):
        await engine.run_step()
    orchestrator.close()


@pytest.mark.asyncio
async def test_caller_cancellation_is_propagated_and_never_replayed(tmp_path, monkeypatch):
    engine, _session, runtime, _candidate, driver, orchestrator = setup_engine(tmp_path, [])

    async def cancel(_context, *, repair_feedback=None):
        driver.calls += 1
        raise asyncio.CancelledError()

    monkeypatch.setattr(driver, "propose", cancel)
    with pytest.raises(asyncio.CancelledError):
        await engine.run_step()
    assert driver.calls == 1 and runtime.progress.active_attempt_id is not None
    reservation = next(
        item
        for item in runtime.operational_budget.reservations
        if item.attempt_id == runtime.progress.active_attempt_id
    )
    assert reservation.status.value == "UNCERTAIN"
    waiting = await engine.run_step()
    assert waiting.outcome is DecisionStepOutcome.WAITING and driver.calls == 1
    orchestrator.close()


@pytest.mark.asyncio
async def test_resolve_action_reuses_committed_batch_after_pre_receipt_crash(
    tmp_path, monkeypatch
):
    session, candidate, req, _secret = fixture()
    reconcile_candidate_state(session, (candidate,))
    resolver = FakeResolver(max_calls=1)
    resolver_registry = registry(resolver)
    driver = ScriptedDriver([])
    cat = catalog()
    clock = FixedClock(datetime(2026, 9, 8, tzinfo=UTC))
    journal = EventJournal(tmp_path / "events.jsonl", run_id="step:resolve", clock=clock)
    composition = build_runtime_composition_fingerprint(
        catalog=cat,
        resolver_descriptors=resolver_registry.descriptors(),
        decision_driver_hash=driver.descriptor.descriptor_hash,
    )
    orchestrator = RuntimeOrchestrator(
        cat,
        composition,
        clock,
        journal,
        AtomicCheckpointStore(tmp_path / "state.json"),
        RuntimeBudgetLimits(max_requests=10, max_tokens=100_000),
    )
    runtime = orchestrator.start()
    DurableResolverDispatcher.initialize(
        session=session,
        runtime=runtime,
        orchestrator=orchestrator,
        limits=OperationalBudgetLimits(max_requests=10, max_tokens=100_000),
    )
    dispatcher = DurableResolverDispatcher(
        session=session,
        runtime=runtime,
        orchestrator=orchestrator,
        registry=resolver_registry,
        permissions=lambda: frozenset({"read"}),
    )
    scheduler = PassiveResolutionScheduler(dispatcher, auth_context=lambda: "principal")
    driver.payloads.append(
        proposal(
            session,
            DecisionAction.RESOLVE_REQUIREMENT,
            requirement_id=req.requirement_id,
        )
    )
    engine = GenericStepEngine(
        session=session,
        runtime=runtime,
        orchestrator=orchestrator,
        driver=driver,
        active_candidate_ids=(candidate.candidate_id,),
        passive_scheduler=scheduler,
        count_tokens=lambda content: len(content.encode("utf-8")) // 4,
    )
    original_record = engine._record

    def crash_before_step_receipt(**_kwargs):
        raise RuntimeError("synthetic crash after semantic commit")

    monkeypatch.setattr(engine, "_record", crash_before_step_receipt)
    with pytest.raises(RuntimeError, match="after semantic commit"):
        await engine.run_step()
    assert resolver.calls == 1 and req.requirement_id in session.knowledge.slots
    monkeypatch.setattr(engine, "_record", original_record)
    engine.authority_epoch = "revoked-after-commit"
    with pytest.raises(RuntimeError, match="no longer matches canonical input"):
        await engine.run_step()
    assert resolver.calls == driver.calls == 1
    engine.authority_epoch = "initial"
    result = await engine.run_step()
    assert result.outcome is DecisionStepOutcome.PROGRESSED
    assert resolver.calls == 1 and req.requirement_id in session.knowledge.slots
    assert runtime.progress.receipts[-1].changed
    orchestrator.close()
