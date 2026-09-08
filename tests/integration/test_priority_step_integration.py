import pytest

from redteam_core.models.decision import DecisionAction, ScopeCompletion
from redteam_core.models.security import CandidateDisposition
from redteam_core.runtime.passive_scheduler import PassiveResolutionScheduler
from redteam_core.runtime.resolver_dispatch import DurableResolverDispatcher
from redteam_core.runtime.step_loop import GenericStepEngine
from tests.integration.test_durable_priority import setup_priority
from tests.integration.test_generic_step_loop import ScriptedDriver, proposal


@pytest.mark.asyncio
@pytest.mark.parametrize("with_domain", [False, True])
async def test_priority_to_fake_step_to_observation_to_bound_decision_to_finish(
    tmp_path, with_domain
):
    from redteam_core.runtime.domain_state import encode_domain_state
    from tests.unit.test_runtime_checkpoint import (
        ReferenceDomainState,
        _domain_state_catalog,
        _domain_state_registry,
    )

    driver = ScriptedDriver([])
    coordinator, providers, permissions, resolver = setup_priority(
        tmp_path,
        count=1,
        with_requirement=True,
        decision_driver=driver,
        extension_catalog=_domain_state_catalog() if with_domain else None,
    )
    session, runtime, manager = coordinator.session, coordinator.runtime, coordinator.orchestrator
    dispatcher = DurableResolverDispatcher(
        session=session,
        runtime=runtime,
        orchestrator=manager,
        registry=coordinator.registry,
        permissions=lambda: frozenset(permissions),
    )
    scheduler = PassiveResolutionScheduler(dispatcher, auth_context=lambda: "synthetic-principal")
    envelopes = ()
    if with_domain:
        envelopes = (
            encode_domain_state(
                bundle_id="reference",
                state=ReferenceDomainState(value="opaque", generation=7),
                catalog=manager.catalog,
                registry=_domain_state_registry(),
            ),
        )
        manager.checkpoint(session, runtime, reason="opaque-domain", domain_states=envelopes)
    try:
        selection = coordinator.select(providers[1], limit=1)
        candidate = session.candidate_state.candidates[selection.selected_candidate_ids[0]]
        driver.payloads.append(
            proposal(
                session,
                DecisionAction.RESOLVE_REQUIREMENT,
                requirement_id=candidate.requirement_ids[0],
            )
        )
        engine = GenericStepEngine(
            session=session,
            runtime=runtime,
            orchestrator=manager,
            driver=driver,
            active_candidate_ids=selection.selected_candidate_ids,
            passive_scheduler=scheduler,
            count_tokens=lambda value: len(value.encode("utf-8")) // 4,
            scope_completion=ScopeCompletion(acquisition_complete=True, search_complete=True),
        )
        first = await engine.run_step()
        assert first.outcome.value == "PROGRESSED"
        persisted = manager.checkpoint_store.load(
            catalog=manager.catalog,
            composition_fingerprint=manager.composition_fingerprint,
            journal=manager.journal,
        )
        assert persisted.domain_states == envelopes
        assert resolver.calls == driver.calls == 1
        assert session.knowledge.slots[candidate.requirement_ids[0]].status.value == "CONSISTENT"
        reassessed = coordinator.select(providers[1], limit=1).assessments[0]
        coordinator.record_decision(
            root_key=reassessed.facts.root_key,
            expected_evidence_hash=reassessed.evidence_hash,
            disposition=CandidateDisposition.REJECTED,
            reason="synthetic host oracle rejected its reference candidate",
        )
        driver.payloads.append(proposal(session, DecisionAction.FINISH, finish_reason="assess"))
        final = await engine.run_step()
        assert final.completion.status.value == "COMPLETE"
        assert driver.calls == 2 and resolver.calls == 1
        assert runtime.usage.requests == 3
        assert runtime.priority.batch_sequence == 2
        assert len(runtime.priority.decisions) == 1
        recovered = manager.resume(expected_run_id=runtime.run_id)
        assert recovered.runtime_state.priority == runtime.priority
        assert recovered.runtime_state.progress == runtime.progress
        assert recovered.domain_states == envelopes
        finalized = await manager.finalize(
            recovered.session, recovered.runtime_state, decision=final.termination
        )
        assert finalized.checkpoint.domain_states == envelopes
    finally:
        await scheduler.close()
        manager.close()
