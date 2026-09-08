from __future__ import annotations

import ast
from datetime import UTC, datetime
from pathlib import Path

import pytest

from redteam_core.acquisition import (
    AcquisitionBudget,
    AcquisitionLimits,
    AnalysisHint,
    ArtifactBoundary,
    search_artifact_text,
)
from redteam_core.execution import (
    ExecutionPolicyViolation,
    ExperimentExecutorRegistry,
    PolicyConstraintEvaluatorRegistry,
    activate_execution_policy,
    authorize_attempt,
    compile_execution_policy,
    execute_authorized_attempt,
    plan_experiment,
)
from redteam_core.extension import compose_bundles
from redteam_core.graph.frontier import FrontierQuery, build_attack_frontiers
from redteam_core.investigation import (
    CandidateDeriverRegistry,
    EvidenceRequirement,
    Observation,
    ResolverRuntimeRegistry,
    RootCauseDecision,
    commit_root_cause_decisions,
    coverage_complete,
    derive_candidates,
    reconcile_candidate_state,
    record_portfolio_selection,
    resolve_and_commit,
    select_candidate_portfolio,
)
from redteam_core.knowledge import (
    CoreSession,
    apply_observation,
    recompute_relations,
    record_expectation,
)
from redteam_core.models import (
    CandidateDisposition,
    CleanupExpectation,
    EffectClass,
    ExpectationRef,
    ExperimentDescriptor,
    Provenance,
    Relation,
    TriState,
    TrustClass,
    WorkingHypothesisUpsert,
    WorkingMemoryPatch,
)
from redteam_core.models.runtime import RuntimeState
from redteam_core.reasoning import apply_working_memory_patch, reconcile_working_memory
from redteam_core.runtime.checkpoint import (
    AtomicCheckpointStore,
    build_runtime_composition_fingerprint,
)
from redteam_core.runtime.journal import EventJournal
from redteam_core.validation import ValidatorRuntimeRegistry, run_validation
from tests.reference_domains import artifact_runtime as artifact
from tests.reference_domains import composite
from tests.reference_domains import request_response as request
from tests.reference_domains import stateful_experiment as stateful
from tests.reference_domains import static_graph as static
from tests.reference_domains.common import MutableClock

NOW = datetime(2026, 9, 8, 3, 0, tzinfo=UTC)
ROOT = Path(__file__).resolve().parents[2]
CORE_FREEZE_SHA = "53e814dcb5152fa178765201a9afa22c22eab928b3ba3b2101645b738cd1c21c"


def _runtime_derivers(*items):
    registry = CandidateDeriverRegistry()
    for item in items:
        registry.register(item)
    registry.freeze()
    return registry


def _runtime_validators(*items):
    registry = ValidatorRuntimeRegistry()
    for item in items:
        registry.register(item)
    registry.freeze()
    return registry


def _runtime_executors(*items):
    registry = ExperimentExecutorRegistry()
    for item in items:
        registry.register(item)
    registry.freeze()
    return registry


def _runtime_policy_evaluators(*items):
    registry = PolicyConstraintEvaluatorRegistry()
    for item in items:
        registry.register(item)
    registry.freeze()
    return registry


def _core_tree_sha() -> str:
    import hashlib

    root = ROOT / "src" / "redteam_core"
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*.py")):
        relative = path.relative_to(root).as_posix().encode()
        digest.update(relative)
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def test_reference_domains_use_the_frozen_core_without_reference_specific_core_edits():
    assert _core_tree_sha() == CORE_FREEZE_SHA


def test_static_graph_reference_proves_unknown_guard_context_bridge_frontier_and_coverage():
    cat = compose_bundles((static.bundle(),))
    session = CoreSession()
    relations = (
        Relation.create(
            source_ref="node:input",
            relation_type_id=static.ENTRY_REL,
            target_ref="node:transform",
            verdict=TriState.TRUE,
            context_refs=["ctx:a"],
        ),
        Relation.create(
            source_ref="node:transform",
            relation_type_id=static.TRANSFORM_REL,
            target_ref="node:bridge",
            verdict=TriState.UNKNOWN,
            missing_reason_ids=["miss_static_unknown"],
            context_refs=["ctx:a"],
        ),
        Relation.create(
            source_ref="node:bridge",
            relation_type_id=static.BRIDGE_REL,
            target_ref="node:execution",
            verdict=TriState.TRUE,
            context_refs=["ctx:b"],
        ),
        Relation.create(
            source_ref="node:execution",
            relation_type_id=static.SINK_REL,
            target_ref="node:sink",
            verdict=TriState.TRUE,
            context_refs=["ctx:b"],
        ),
        Relation.create(
            source_ref="guard:policy",
            relation_type_id=static.GUARD_REL,
            target_ref="node:sink",
            verdict=TriState.UNKNOWN,
            context_refs=["ctx:b"],
        ),
    )
    session.semantic.relations = {item.relation_id: item for item in relations}
    search = build_attack_frontiers(
        session,
        catalog=cat,
        query=FrontierQuery(sink_hints=("sensitive",)),
    )
    complete = [item for item in search.frontiers if item.sink_ref == "node:sink"]
    assert complete
    frontier = complete[0]
    assert frontier.unknown_relation_ids
    assert frontier.uncertain_guard_relation_ids
    assert {"ctx:a", "ctx:b"} <= set(frontier.context_refs)

    candidates = derive_candidates(
        session,
        catalog=cat,
        registry=_runtime_derivers(static.FrontierCandidateDeriver()),
    )
    reconcile_candidate_state(session, candidates)
    assert len(candidates) == 1
    portfolio = select_candidate_portfolio(session, limit=1)
    record_portfolio_selection(session, portfolio)
    commit_root_cause_decisions(
        session,
        portfolio,
        (
            RootCauseDecision(
                root_cause_key=portfolio.entries[0].root_cause_key,
                disposition=CandidateDisposition.ACCEPTED,
                reason="synthetic static flow reviewed",
            ),
        ),
    )
    assert coverage_complete(session)


@pytest.mark.asyncio
async def test_stateful_reference_proves_pathless_candidate_and_observed_not_normative():
    cat = compose_bundles((stateful.bundle(),))
    clock = MutableClock(NOW)
    session = CoreSession()
    expectation_registration = cat.expectation_types.require(stateful.EXPECTATION_TYPE)
    baseline_payload = expectation_registration.envelope_from_model(
        stateful.BaselinePayload(expected_before=10, expected_after=10)
    )
    inferred = ExpectationRef.create(
        expectation_type_id=stateful.EXPECTATION_TYPE,
        payload=baseline_payload,
        provenance=Provenance(
            source_kind="llm",
            producer_id="refstate.planner",
            trust_class=TrustClass.INFERRED,
            acquired_at=NOW,
            content_hash=baseline_payload.content_hash,
        ),
    )
    record_expectation(session, inferred, catalog=cat)

    derivers = _runtime_derivers(stateful.PathlessDeltaDeriver())
    candidates = derive_candidates(session, catalog=cat, registry=derivers)
    reconcile_candidate_state(session, candidates)
    candidate = candidates[0]
    assert candidate.supporting_relation_ids == []

    action = cat.experiment_actions.require(stateful.ACTION_TYPE).envelope_from_model(
        stateful.MutationAction(delta=5)
    )
    descriptor = ExperimentDescriptor.create(
        action_type_id=stateful.ACTION_TYPE,
        subject_ref="state:counter",
        target_refs=("state:counter",),
        effect_class=EffectClass.STATE_CHANGING,
        cleanup_expectation=CleanupExpectation.NONE,
        input_payload=action,
    )
    constraint = cat.policy_constraints.require(stateful.CONSTRAINT_TYPE).envelope_from_model(
        stateful.DeltaConstraint(max_abs_delta=10)
    )
    evaluators = _runtime_policy_evaluators(stateful.DeltaPolicyEvaluator())
    policy = compile_execution_policy(
        catalog=cat,
        approval_ref="approval:stateful",
        approved_at=NOW,
        allowed_scope_refs=frozenset({"state:counter"}),
        allowed_effect_classes=frozenset({EffectClass.STATE_CHANGING}),
        allowed_action_type_ids=frozenset({stateful.ACTION_TYPE}),
        cleanup_required=False,
        constraints=(constraint,),
    )
    activate_execution_policy(session, policy=policy, catalog=cat, clock=clock)
    attempt = plan_experiment(session, descriptor=descriptor, catalog=cat, clock=clock)
    authorize_attempt(
        session,
        attempt_id=attempt.attempt_id,
        policy=policy,
        catalog=cat,
        evaluators=evaluators,
        clock=clock,
    )
    terminal, result = await execute_authorized_attempt(
        session,
        attempt_id=attempt.attempt_id,
        policy=policy,
        catalog=cat,
        evaluators=evaluators,
        executors=_runtime_executors(stateful.CounterExecutor(cat)),
        clock=clock,
    )
    assert terminal.status.value == "SUCCEEDED"
    assert result is not None

    validators = _runtime_validators(stateful.DeltaInvariantValidator())
    first_run = run_validation(
        session,
        candidate=session.candidate_state.candidates[candidate.candidate_id],
        experiment_result=result,
        catalog=cat,
        registry=validators,
    )
    assert {session.validation.outcomes[item].verdict.value for item in first_run.outcome_ids} == {
        "UNKNOWN"
    }

    authoritative = ExpectationRef.create(
        expectation_type_id=stateful.EXPECTATION_TYPE,
        payload=baseline_payload,
        provenance=Provenance(
            source_kind="baseline",
            producer_id="refstate.baseline",
            trust_class=TrustClass.OBSERVED,
            acquired_at=NOW,
            content_hash=baseline_payload.content_hash,
        ),
    )
    record_expectation(session, authoritative, catalog=cat)
    refreshed = derive_candidates(session, catalog=cat, registry=derivers)
    reconcile_candidate_state(session, refreshed)
    second_run = run_validation(
        session,
        candidate=session.candidate_state.candidates[candidate.candidate_id],
        experiment_result=result,
        catalog=cat,
        registry=validators,
    )
    assert second_run.run_id != first_run.run_id
    verdicts = {session.validation.outcomes[item].verdict.value for item in second_run.outcome_ids}
    assert verdicts == {"VIOLATION"}
    assert second_run.finding_ids


@pytest.mark.asyncio
async def test_artifact_runtime_reference_proves_hint_then_runtime_observation(tmp_path):
    (tmp_path / "sample.txt").write_text("ordinary\nRUNTIME_CHECK marker\n", encoding="utf-8")
    boundary = ArtifactBoundary(tmp_path)
    budget = AcquisitionBudget(
        limits=AcquisitionLimits(
            max_scanned_files=10,
            max_total_bytes_read=10_000,
            max_single_file_bytes=10_000,
        )
    )
    search = search_artifact_text(boundary, budget, "RUNTIME_CHECK")
    assert search.hits and search.hits[0].content_trust == "UNTRUSTED_ARTIFACT"
    hint = AnalysisHint.create(
        hint_type_id="refartifact.hint.runtime@1",
        subject_ref=search.hits[0].relative_path,
        score=900,
        reason="static marker deserves runtime verification",
    )
    assert hint.advisory_only

    cat = compose_bundles((artifact.bundle(),))
    session = CoreSession()
    requirement = EvidenceRequirement.create(
        requirement_type_id=artifact.REQUIREMENT_TYPE,
        provider_id="refartifact.fake",
        subject_ref="artifact:entry",
        reason="verify static suspicion at runtime",
    )
    session.investigation.requirements[requirement.requirement_id] = requirement
    recomputer = artifact.RuntimeRecomputer(requirement.requirement_id)
    recompute_relations(session, catalog=cat, recomputers=(recomputer,))
    initial = next(iter(session.semantic.relations.values()))
    assert initial.verdict is TriState.UNKNOWN

    derivers = _runtime_derivers(artifact.RuntimeCandidateDeriver())
    before = derive_candidates(session, catalog=cat, registry=derivers)
    assert before[0].current_claim_status.value == "CANDIDATE"
    reconcile_candidate_state(session, before)
    relation_id = next(iter(session.semantic.relations))
    memory_update = apply_working_memory_patch(
        session,
        WorkingMemoryPatch(
            expected_revision=0,
            upserts=(
                WorkingHypothesisUpsert(
                    semantic_key="artifact-runtime-investigation",
                    candidate_id=before[0].candidate_id,
                    claim="Runtime verification should decide the static suspicion.",
                    supporting_relation_ids=(relation_id,),
                    decision_summary="Waiting for authoritative runtime evidence.",
                ),
            ),
        ),
    )
    assert memory_update.accepted and memory_update.changed
    blocked = reconcile_working_memory(session)
    assert blocked.blocked_hypothesis_ids

    resolvers = ResolverRuntimeRegistry()
    resolvers.register(artifact.FakeRuntimeResolver())
    resolvers.freeze()
    _, commit = await resolve_and_commit(
        session=session,
        requirement=requirement,
        resolver_registry=resolvers,
        catalog=cat,
        clock=MutableClock(NOW),
        expected_revision=0,
        recomputers=(recomputer,),
    )
    assert commit is not None and commit.semantic_changed
    current = next(iter(session.semantic.relations.values()))
    assert current.verdict is TriState.TRUE
    after = derive_candidates(session, catalog=cat, registry=derivers)
    assert after[0].current_claim_status.value == "SUPPORTED"
    reconcile_candidate_state(session, after)
    ready = reconcile_working_memory(session)
    assert ready.ready_hypothesis_ids
    stable_candidate_state = session.candidate_state.model_dump(mode="json")
    stable_memory = session.working_memory.model_dump(mode="json")
    reconcile_candidate_state(session, derive_candidates(session, catalog=cat, registry=derivers))
    reconcile_working_memory(session)
    assert session.candidate_state.model_dump(mode="json") == stable_candidate_state
    assert session.working_memory.model_dump(mode="json") == stable_memory


@pytest.mark.asyncio
async def test_request_response_reference_keeps_not_found_unknown_and_separates_policy_effects():
    cat = compose_bundles((request.bundle(),))
    clock = MutableClock(NOW)
    session = CoreSession()
    requirement = EvidenceRequirement.create(
        requirement_type_id=request.REQUIREMENT_TYPE,
        provider_id="refrequest.fake",
        subject_ref="resource:other-owner",
        reason="determine cross-principal authorization",
    )
    session.investigation.requirements[requirement.requirement_id] = requirement
    recomputer = request.AuthorizationRecomputer(requirement.requirement_id)
    recompute_relations(session, catalog=cat, recomputers=(recomputer,))
    relation = next(iter(session.semantic.relations.values()))
    assert relation.verdict is TriState.UNKNOWN

    resolvers = ResolverRuntimeRegistry()
    resolvers.register(request.NotFoundResolver())
    resolvers.freeze()
    resolution, commit = await resolve_and_commit(
        session=session,
        requirement=requirement,
        resolver_registry=resolvers,
        catalog=cat,
        clock=clock,
        expected_revision=0,
        recomputers=(recomputer,),
    )
    assert commit is None and resolution.failure is not None
    assert resolution.failure.kind.value == "AMBIGUOUS_NOT_FOUND"
    assert next(iter(session.semantic.relations.values())).verdict is TriState.UNKNOWN

    derivers = _runtime_derivers(request.AuthorizationDeriver())
    candidates = derive_candidates(session, catalog=cat, registry=derivers)
    reconcile_candidate_state(session, candidates)
    candidate = candidates[0]

    read_policy = compile_execution_policy(
        catalog=cat,
        approval_ref="approval:read-only",
        approved_at=NOW,
        allowed_scope_refs=frozenset({"principal:attacker", "resource:other-owner"}),
        allowed_effect_classes=frozenset({EffectClass.READ_ONLY}),
        allowed_action_type_ids=frozenset({request.READ_ACTION}),
        cleanup_required=False,
    )
    empty_evaluators = _runtime_policy_evaluators()
    activate_execution_policy(session, policy=read_policy, catalog=cat, clock=clock)
    write_payload = cat.experiment_actions.require(request.WRITE_ACTION).envelope_from_model(
        request.RequestAction(resource_ref="resource:other-owner")
    )
    write_descriptor = ExperimentDescriptor.create(
        action_type_id=request.WRITE_ACTION,
        subject_ref="principal:attacker",
        target_refs=("resource:other-owner",),
        effect_class=EffectClass.STATE_CHANGING,
        input_payload=write_payload,
    )
    attempt = plan_experiment(session, descriptor=write_descriptor, catalog=cat, clock=clock)
    with pytest.raises(ExecutionPolicyViolation):
        authorize_attempt(
            session,
            attempt_id=attempt.attempt_id,
            policy=read_policy,
            catalog=cat,
            evaluators=empty_evaluators,
            clock=clock,
        )

    state_policy = compile_execution_policy(
        catalog=cat,
        approval_ref="approval:state-change",
        approved_at=NOW,
        allowed_scope_refs=frozenset({"principal:attacker", "resource:other-owner"}),
        allowed_effect_classes=frozenset({EffectClass.STATE_CHANGING}),
        allowed_action_type_ids=frozenset({request.WRITE_ACTION}),
        cleanup_required=False,
    )
    activate_execution_policy(session, policy=state_policy, catalog=cat, clock=clock)
    authorize_attempt(
        session,
        attempt_id=attempt.attempt_id,
        policy=state_policy,
        catalog=cat,
        evaluators=empty_evaluators,
        clock=clock,
    )
    _, result = await execute_authorized_attempt(
        session,
        attempt_id=attempt.attempt_id,
        policy=state_policy,
        catalog=cat,
        evaluators=empty_evaluators,
        executors=_runtime_executors(request.FakeRequestExecutor(cat)),
        clock=clock,
    )
    assert result is not None and result.result_payload is not None

    access = request.AccessResult.model_validate_json(result.result_payload.canonical_payload_json)
    observation_registration = cat.observation_payloads.require(request.OBSERVATION_TYPE)
    observation_payload = observation_registration.envelope_from_model(
        request.AccessObservation(granted=access.granted, response_code=access.response_code)
    )
    observation = Observation.create(
        requirement_id=requirement.requirement_id,
        resolver_id="refrequest.experiment_adapter@1",
        subject_ref=requirement.subject_ref,
        payload=observation_payload,
        provenance=Provenance(
            source_kind="experiment",
            producer_id="refrequest.experiment_adapter@1",
            trust_class=TrustClass.OBSERVED,
            acquired_at=NOW,
            content_hash=observation_payload.content_hash,
        ),
    )
    apply_observation(
        session,
        observation,
        catalog=cat,
        evaluation_time=NOW,
        expected_revision=0,
        recomputers=(recomputer,),
    )
    assert next(iter(session.semantic.relations.values())).verdict is TriState.TRUE

    refreshed = derive_candidates(session, catalog=cat, registry=derivers)
    reconcile_candidate_state(session, refreshed)
    run = run_validation(
        session,
        candidate=session.candidate_state.candidates[candidate.candidate_id],
        experiment_result=result,
        catalog=cat,
        registry=_runtime_validators(request.AuthorizationValidator()),
    )
    assert {session.validation.outcomes[item].verdict.value for item in run.outcome_ids} == {
        "VIOLATION"
    }


def test_composite_reference_composition_is_order_independent_and_integration_is_public_only():
    bundles = (
        static.bundle(),
        stateful.bundle(),
        artifact.bundle(),
        request.bundle(),
        composite.bundle(),
    )
    first = compose_bundles(bundles)
    second = compose_bundles(tuple(reversed(bundles)))
    assert first.semantic_manifest_hash == second.semantic_manifest_hash
    assert set(first.bundle_manifests) == {
        "refstatic",
        "refstate",
        "refartifact",
        "refrequest",
        "refintegration",
    }
    assert first.relation_types.require(composite.RELATION_TYPE).owner_bundle_id == "refintegration"

    without_static = compose_bundles(
        (stateful.bundle(), artifact.bundle(), request.bundle(), composite.bundle())
    )
    assert first.candidate_types.require(stateful.CANDIDATE_TYPE).manifest_record() == (
        without_static.candidate_types.require(stateful.CANDIDATE_TYPE).manifest_record()
    )
    assert first.requirement_types.require(request.REQUIREMENT_TYPE).manifest_record() == (
        without_static.requirement_types.require(request.REQUIREMENT_TYPE).manifest_record()
    )

    full_session = CoreSession()
    reduced_session = CoreSession()
    for target_session, target_catalog in (
        (full_session, first),
        (reduced_session, without_static),
    ):
        baseline = target_catalog.expectation_types.require(
            stateful.EXPECTATION_TYPE
        ).envelope_from_model(stateful.BaselinePayload(expected_before=10, expected_after=10))
        record_expectation(
            target_session,
            ExpectationRef.create(
                expectation_type_id=stateful.EXPECTATION_TYPE,
                payload=baseline,
                provenance=Provenance(
                    source_kind="baseline",
                    producer_id="refstate.bundle-removal-proof",
                    trust_class=TrustClass.OBSERVED,
                    acquired_at=NOW,
                    content_hash=baseline.content_hash,
                ),
            ),
            catalog=target_catalog,
        )
    full_candidates = derive_candidates(
        full_session,
        catalog=first,
        registry=_runtime_derivers(
            static.FrontierCandidateDeriver(),
            stateful.PathlessDeltaDeriver(),
            artifact.RuntimeCandidateDeriver(),
            request.AuthorizationDeriver(),
        ),
    )
    reduced_candidates = derive_candidates(
        reduced_session,
        catalog=without_static,
        registry=_runtime_derivers(
            stateful.PathlessDeltaDeriver(),
            artifact.RuntimeCandidateDeriver(),
            request.AuthorizationDeriver(),
        ),
    )
    assert tuple(
        item for item in full_candidates if item.candidate_type_id == stateful.CANDIDATE_TYPE
    ) == tuple(
        item for item in reduced_candidates if item.candidate_type_id == stateful.CANDIDATE_TYPE
    )

    source = (ROOT / "tests" / "reference_domains" / "composite.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    sibling_private_imports = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            if node.module.startswith(
                (
                    "tests.reference_domains.static_graph",
                    "tests.reference_domains.stateful_experiment",
                    "tests.reference_domains.artifact_runtime",
                    "tests.reference_domains.request_response",
                )
            ):
                sibling_private_imports.append(node.module)
    assert not sibling_private_imports


def test_composite_runtime_candidate_validator_and_checkpoint_are_deterministic(tmp_path):
    bundles = (
        static.bundle(),
        stateful.bundle(),
        artifact.bundle(),
        request.bundle(),
        composite.bundle(),
    )
    cat = compose_bundles(bundles)
    reverse_cat = compose_bundles(tuple(reversed(bundles)))
    assert cat.semantic_manifest_hash == reverse_cat.semantic_manifest_hash

    session = CoreSession()
    static_relations = (
        Relation.create(
            source_ref="composite:input",
            relation_type_id=static.ENTRY_REL,
            target_ref="composite:transform",
            verdict=TriState.TRUE,
            context_refs=["ctx:composite"],
        ),
        Relation.create(
            source_ref="composite:transform",
            relation_type_id=static.SINK_REL,
            target_ref="composite:sink",
            verdict=TriState.TRUE,
            context_refs=["ctx:composite"],
        ),
        Relation.create(
            source_ref="artifact:entry",
            relation_type_id=composite.RELATION_TYPE,
            target_ref="resource:other-owner",
            verdict=TriState.TRUE,
            context_refs=["ctx:integration"],
        ),
    )
    session.semantic.relations = {item.relation_id: item for item in static_relations}

    state_payload = cat.expectation_types.require(stateful.EXPECTATION_TYPE).envelope_from_model(
        stateful.BaselinePayload(expected_before=10, expected_after=10)
    )
    record_expectation(
        session,
        ExpectationRef.create(
            expectation_type_id=stateful.EXPECTATION_TYPE,
            payload=state_payload,
            provenance=Provenance(
                source_kind="baseline",
                producer_id="refstate.composite_baseline",
                trust_class=TrustClass.OBSERVED,
                acquired_at=NOW,
                content_hash=state_payload.content_hash,
            ),
        ),
        catalog=cat,
    )

    artifact_requirement = EvidenceRequirement.create(
        requirement_type_id=artifact.REQUIREMENT_TYPE,
        provider_id="refartifact.fake",
        subject_ref="artifact:entry",
        reason="composite artifact runtime verification",
    )
    request_requirement = EvidenceRequirement.create(
        requirement_type_id=request.REQUIREMENT_TYPE,
        provider_id="refrequest.fake",
        subject_ref="resource:other-owner",
        reason="composite authorization verification",
    )
    session.investigation.requirements = {
        artifact_requirement.requirement_id: artifact_requirement,
        request_requirement.requirement_id: request_requirement,
    }
    recompute_relations(
        session,
        catalog=cat,
        recomputers=(
            artifact.RuntimeRecomputer(artifact_requirement.requirement_id),
            request.AuthorizationRecomputer(request_requirement.requirement_id),
        ),
    )

    derivers = _runtime_derivers(
        static.FrontierCandidateDeriver(),
        stateful.PathlessDeltaDeriver(),
        artifact.RuntimeCandidateDeriver(),
        request.AuthorizationDeriver(),
    )
    first_candidates = derive_candidates(session, catalog=cat, registry=derivers)
    second_candidates = derive_candidates(session, catalog=reverse_cat, registry=derivers)
    assert first_candidates == second_candidates
    reconcile_candidate_state(session, first_candidates)
    first_portfolio = select_candidate_portfolio(session, limit=4)
    second_portfolio = select_candidate_portfolio(session, limit=4)
    assert first_portfolio == second_portfolio
    assert len(first_portfolio.entries) >= 4

    validators = _runtime_validators(
        stateful.DeltaInvariantValidator(),
        request.AuthorizationValidator(),
    )
    state_candidate = next(
        item for item in first_candidates if item.candidate_type_id == stateful.CANDIDATE_TYPE
    )
    run_one = run_validation(
        session,
        candidate=session.candidate_state.candidates[state_candidate.candidate_id],
        catalog=cat,
        registry=validators,
    )
    run_two = run_validation(
        session,
        candidate=session.candidate_state.candidates[state_candidate.candidate_id],
        catalog=cat,
        registry=validators,
    )
    assert run_one == run_two
    assert {session.validation.outcomes[item].verdict.value for item in run_one.outcome_ids} == {
        "UNKNOWN"
    }

    clock = MutableClock(NOW)
    journal = EventJournal(tmp_path / "events.jsonl", run_id="composite-run", clock=clock)
    store = AtomicCheckpointStore(tmp_path / "checkpoint.json")
    composition = build_runtime_composition_fingerprint(catalog=cat)
    written = store.write(
        session=session,
        runtime_state=RuntimeState.create("composite-run"),
        catalog=cat,
        composition_fingerprint=composition,
        journal=journal,
        clock=clock,
        reason="composite-proof",
    )
    restored = store.load(
        catalog=cat,
        composition_fingerprint=composition,
        journal=journal,
        expected_run_id="composite-run",
    )
    assert restored.state_hash == written.state_hash
    assert restored.session.candidate_state == session.candidate_state
