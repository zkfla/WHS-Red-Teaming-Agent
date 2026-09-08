from datetime import UTC, datetime, timedelta

import pytest

from redteam_core.extension import compose_bundles
from redteam_core.investigation import (
    CandidateDeriverRegistry,
    EvidenceFreshness,
    EvidenceRequirement,
    ResolverRuntimeRegistry,
    derive_candidates,
    reconcile_candidate_state,
)
from redteam_core.knowledge import CoreSession
from redteam_core.models import (
    DecisionAction,
    DecisionStepOutcome,
    EffectClass,
    ExperimentDescriptor,
    Relation,
    TriState,
)
from redteam_core.models.operational_budget import OperationalBudgetLimits
from redteam_core.models.runtime import RuntimeBudgetLimits
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
from tests.integration.test_generic_step_loop import ScriptedDriver, proposal
from tests.reference_domains import artifact_runtime as artifact
from tests.reference_domains import request_response as request
from tests.reference_domains import stateful_experiment as stateful
from tests.reference_domains import static_graph as static

NOW = datetime(2026, 9, 8, tzinfo=UTC)


def derive(session, cat, deriver):
    registry = CandidateDeriverRegistry()
    registry.register(deriver)
    registry.freeze()
    candidates = derive_candidates(session, catalog=cat, registry=registry)
    reconcile_candidate_state(session, candidates)
    assert len(candidates) == 1
    return candidates[0]


def static_case():
    cat = compose_bundles((static.bundle(),))
    session = CoreSession()
    relations = (
        Relation.create(
            source_ref="source",
            relation_type_id=static.ENTRY_REL,
            target_ref="middle",
            verdict=TriState.TRUE,
        ),
        Relation.create(
            source_ref="middle",
            relation_type_id=static.SINK_REL,
            target_ref="sink",
            verdict=TriState.TRUE,
        ),
    )
    session.semantic.relations = {item.relation_id: item for item in relations}
    return cat, session, derive(session, cat, static.FrontierCandidateDeriver())


def stateful_case():
    cat = compose_bundles((stateful.bundle(),))
    session = CoreSession()
    return cat, session, derive(session, cat, stateful.PathlessDeltaDeriver())


def artifact_case():
    cat = compose_bundles((artifact.bundle(),))
    session = CoreSession()
    req = EvidenceRequirement.create(
        requirement_type_id=artifact.REQUIREMENT_TYPE,
        provider_id="refartifact.fake",
        subject_ref="runtime:sink",
        reason="reference runtime check",
        freshness=EvidenceFreshness(max_age_seconds=1),
    )
    session.investigation.requirements[req.requirement_id] = req
    relation = Relation.create(
        source_ref="artifact:entry",
        relation_type_id=artifact.RELATION_TYPE,
        target_ref="runtime:sink",
        requirement_ids=[req.requirement_id],
    )
    session.semantic.relations[relation.relation_id] = relation
    return cat, session, derive(session, cat, artifact.RuntimeCandidateDeriver())


def request_case():
    cat = compose_bundles((request.bundle(),))
    session = CoreSession()
    req = EvidenceRequirement.create(
        requirement_type_id=request.REQUIREMENT_TYPE,
        provider_id="refrequest.fake",
        subject_ref="resource:other-owner",
        reason="reference authorization check",
    )
    session.investigation.requirements[req.requirement_id] = req
    relation = Relation.create(
        source_ref="principal:attacker",
        relation_type_id=request.RELATION_TYPE,
        target_ref="resource:other-owner",
        requirement_ids=[req.requirement_id],
    )
    session.semantic.relations[relation.relation_id] = relation
    return cat, session, derive(session, cat, request.AuthorizationDeriver())


@pytest.mark.asyncio
@pytest.mark.parametrize("case", [static_case, stateful_case, artifact_case, request_case])
async def test_provider_free_step_loop_preserves_each_reference_domain(case, tmp_path):
    cat, session, candidate = case()
    driver = ScriptedDriver([])
    composition = build_runtime_composition_fingerprint(
        catalog=cat, decision_driver_hash=driver.descriptor.descriptor_hash
    )
    journal = EventJournal(
        tmp_path / "events.jsonl", run_id="reference:step", clock=FixedClock(NOW)
    )
    orchestrator = RuntimeOrchestrator(
        cat,
        composition,
        FixedClock(NOW),
        journal,
        AtomicCheckpointStore(tmp_path / "state.json"),
        RuntimeBudgetLimits(max_requests=2, max_tokens=20_000),
    )
    runtime = orchestrator.start()
    DurableResolverDispatcher.initialize(
        session=session,
        runtime=runtime,
        orchestrator=orchestrator,
        limits=OperationalBudgetLimits(max_requests=2, max_tokens=20_000),
    )
    driver.payloads.append(
        proposal(session, DecisionAction.FINISH, finish_reason="reference assessment")
    )
    result = await GenericStepEngine(
        session=session,
        runtime=runtime,
        orchestrator=orchestrator,
        driver=driver,
        active_candidate_ids=(candidate.candidate_id,),
        count_tokens=lambda content: len(content.encode("utf-8")) // 4,
    ).run_step()
    assert result.outcome is DecisionStepOutcome.FINISH_REQUESTED
    assert result.completion.status.value == "PARTIAL"
    assert session.candidate_state.candidates[candidate.candidate_id] == candidate
    assert runtime.progress.receipts[-1].proposal_id == result.receipt.proposal_id
    orchestrator.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case", "action_kind"),
    [
        (static_case, "context"),
        (stateful_case, "experiment"),
        (artifact_case, "resolve_success"),
        (request_case, "resolve_failure"),
    ],
)
async def test_reference_domains_run_their_real_bounded_investigation_action(
    case, action_kind, tmp_path
):
    cat, session, candidate = case()
    driver = ScriptedDriver([])
    resolver_registry = ResolverRuntimeRegistry()
    if action_kind == "resolve_success":
        resolver = artifact.FakeRuntimeResolver()
        resolver_registry.register(
            artifact.FakeRuntimeResolver(
                descriptor=resolver.descriptor.model_copy(
                    update={"max_tokens_per_call": 0}
                )
            )
        )
    elif action_kind == "resolve_failure":
        resolver = request.NotFoundResolver()
        resolver_registry.register(
            request.NotFoundResolver(
                descriptor=resolver.descriptor.model_copy(
                    update={"max_tokens_per_call": 0}
                )
            )
        )
    resolver_registry.freeze()
    recomputers = (
        (artifact.RuntimeRecomputer(candidate.requirement_ids[0]),)
        if action_kind == "resolve_success" else ()
    )
    pipeline_hash = "sha256:" + "7" * 64 if recomputers else None
    if recomputers:
        session.semantic.relation_producers[candidate.supporting_relation_ids[0]] = (
            artifact.RECOMPUTER_ID
        )
    composition = build_runtime_composition_fingerprint(
        catalog=cat,
        resolver_descriptors=resolver_registry.descriptors(),
        decision_driver_hash=driver.descriptor.descriptor_hash,
        semantic_pipeline_hash=pipeline_hash,
    )
    clock = FixedClock(NOW)
    orchestrator = RuntimeOrchestrator(
        cat,
        composition,
        clock,
        EventJournal(tmp_path / "actions.jsonl", run_id="reference:actions", clock=clock),
        AtomicCheckpointStore(tmp_path / "actions.json"),
        RuntimeBudgetLimits(max_requests=4, max_tokens=40_000),
    )
    runtime = orchestrator.start()
    DurableResolverDispatcher.initialize(
        session=session,
        runtime=runtime,
        orchestrator=orchestrator,
        limits=OperationalBudgetLimits(max_requests=4, max_tokens=40_000),
    )
    scheduler = None
    experiment_plans = None
    if action_kind.startswith("resolve"):
        dispatcher = DurableResolverDispatcher(
            session=session,
            runtime=runtime,
            orchestrator=orchestrator,
            registry=resolver_registry,
            permissions=lambda: frozenset(),
        )
        scheduler = PassiveResolutionScheduler(
            dispatcher, auth_context=lambda: "reference-principal"
        )
        action = proposal(
            session,
            DecisionAction.RESOLVE_REQUIREMENT,
            requirement_id=candidate.requirement_ids[0],
        )
    elif action_kind == "context":
        action = proposal(
            session,
            DecisionAction.QUERY_CONTEXT,
            candidate_id=candidate.candidate_id,
            retrieval_ref=f"candidate:{candidate.candidate_id}:relations",
        )
    else:
        payload = cat.experiment_actions.require(stateful.ACTION_TYPE).envelope_from_model(
            stateful.MutationAction(delta=1)
        )
        descriptor = ExperimentDescriptor.create(
            action_type_id=stateful.ACTION_TYPE,
            subject_ref="state:counter",
            target_refs=("state:counter",),
            effect_class=EffectClass.STATE_CHANGING,
            input_payload=payload,
        )
        experiment_plans = {candidate.candidate_id: descriptor}
        action = proposal(
            session,
            DecisionAction.REQUEST_EXPERIMENT_PLAN,
            candidate_id=candidate.candidate_id,
        )
    driver.payloads.append(action)
    engine = GenericStepEngine(
        session=session,
        runtime=runtime,
        orchestrator=orchestrator,
        driver=driver,
        active_candidate_ids=(candidate.candidate_id,),
        passive_scheduler=scheduler,
        recomputers=recomputers,
        semantic_pipeline_hash=pipeline_hash,
        experiment_plans=experiment_plans,
        count_tokens=lambda content: len(content.encode("utf-8")) // 4,
    )
    result = await engine.run_step()
    if action_kind == "context":
        assert result.detail == "CONTEXT_RETRIEVED"
        assert runtime.progress.context_retrievals
    elif action_kind == "experiment":
        assert result.outcome is DecisionStepOutcome.PROGRESSED
        assert next(iter(session.execution.attempts.values())).status.value == "PLANNED"
    elif action_kind == "resolve_success":
        assert result.outcome is DecisionStepOutcome.PROGRESSED
        assert candidate.requirement_ids[0] in session.knowledge.slots
        relation = session.semantic.relations[candidate.supporting_relation_ids[0]]
        assert relation.verdict is TriState.TRUE
        assert session.semantic.relation_producers[relation.relation_id] == artifact.RECOMPUTER_ID
        orchestrator.clock = FixedClock(NOW + timedelta(seconds=2))

        async def finish_after_refresh(context, *, repair_feedback=None):
            driver.calls += 1
            from redteam_core.models import DecisionDriverReply
            return DecisionDriverReply(payload=proposal(
                session, DecisionAction.FINISH, finish_reason="after expiry"
            ))

        driver.propose = finish_after_refresh
        finished = await engine.run_step()
        assert finished.completion.status.value == "PARTIAL"
        assert "UNRESOLVED_REQUIREMENTS" in finished.completion.reasons
        assert session.semantic.relations[relation.relation_id].verdict is TriState.UNKNOWN
    else:
        assert result.outcome is DecisionStepOutcome.PROGRESSED
        failure = next(iter(session.investigation.resolution_failures.values()))
        assert failure.kind.value == "AMBIGUOUS_NOT_FOUND"
    if scheduler is not None:
        await scheduler.close()
    orchestrator.close()
