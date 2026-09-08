import pytest

from redteam_core.investigation.coverage import reconcile_candidate_state
from redteam_core.investigation.priority import (
    BaselinePriorityProvider,
    ExpectedUtilityPriorityProvider,
)
from redteam_core.models.operational_budget import OperationalBudgetLimits
from redteam_core.models.runtime import RuntimeBudgetLimits
from redteam_core.models.security import CandidateDisposition
from redteam_core.runtime import FixedClock
from redteam_core.runtime.checkpoint import (
    AtomicCheckpointStore,
    build_runtime_composition_fingerprint,
)
from redteam_core.runtime.journal import EventJournal
from redteam_core.runtime.orchestrator import RuntimeOrchestrator
from redteam_core.runtime.priority import PriorityCoordinator
from redteam_core.runtime.resolver_dispatch import DurableResolverDispatcher
from tests.helpers import catalog, requirement
from tests.integration.test_durable_resolver_dispatch import FakeResolver, registry
from tests.unit.test_priority import NOW, pool


def setup_priority(
    tmp_path,
    *,
    count=4,
    requests=20,
    with_requirement=False,
    decision_driver=None,
    extension_catalog=None,
):
    session, _, _ = pool(count)
    if with_requirement:
        req = requirement()
        session.investigation.requirements[req.requirement_id] = req
        reconcile_candidate_state(
            session,
            tuple(
                row.model_copy(
                    update={
                        "requirement_ids": [req.requirement_id],
                    }
                )
                for row in session.candidate_state.candidates.values()
            ),
        )
    resolver = FakeResolver(max_calls=20)
    resolvers = registry(resolver)
    providers = BaselinePriorityProvider(), ExpectedUtilityPriorityProvider()
    cat = extension_catalog or catalog()
    composition = build_runtime_composition_fingerprint(
        catalog=cat,
        resolver_descriptors=resolvers.descriptors(),
        priority_provider_ids=tuple(provider.descriptor.binding_id for provider in providers),
        decision_driver_hash=decision_driver.descriptor.descriptor_hash
        if decision_driver
        else None,
    )
    orchestrator = RuntimeOrchestrator(
        cat,
        composition,
        FixedClock(NOW),
        EventJournal(tmp_path / "events.jsonl", run_id="priority:test", clock=FixedClock(NOW)),
        AtomicCheckpointStore(tmp_path / "state.json"),
        RuntimeBudgetLimits(max_requests=max(1, requests)),
    )
    runtime = orchestrator.start()
    DurableResolverDispatcher.initialize(
        session=session,
        runtime=runtime,
        orchestrator=orchestrator,
        limits=OperationalBudgetLimits(
            max_requests=requests, max_tokens=40000 if decision_driver else 1000
        ),
    )
    permissions = {"read"}
    coordinator = PriorityCoordinator(
        session=session,
        runtime=runtime,
        orchestrator=orchestrator,
        registry=resolvers,
        permissions=lambda: frozenset(permissions),
    )
    return coordinator, providers, permissions, resolver


def test_selection_restart_and_explicit_baseline_rollback_preserve_aging(tmp_path):
    coordinator, providers, _, resolver = setup_priority(tmp_path)
    try:
        first = coordinator.select(providers[1], limit=2)
        second = coordinator.select(providers[0], limit=2)
        assert first.provider_binding != second.provider_binding
        assert coordinator.runtime.priority.batch_sequence == 2
        snapshot = coordinator.runtime.priority.model_dump(mode="json")
        resumed = coordinator.orchestrator.resume(expected_run_id="priority:test")
        assert resumed.runtime_state.priority.model_dump(mode="json") == snapshot
        coordinator.session, coordinator.runtime = resumed.session, resumed.runtime_state
        coordinator.select(providers[1], limit=2)
        assert coordinator.runtime.priority.batch_sequence == 3
        assert sum(row.selection_count for row in coordinator.runtime.priority.roots.values()) == 6
        assert resolver.calls == 0
        assert coordinator.runtime.operational_budget.charged_requests == 0
    finally:
        coordinator.orchestrator.close()


@pytest.mark.parametrize("save_first", [False, True])
def test_priority_storage_failure_is_atomic_and_poisoned(tmp_path, monkeypatch, save_first):
    coordinator, providers, _, _ = setup_priority(tmp_path)
    original = coordinator.orchestrator.checkpoint
    before = coordinator.runtime.model_dump(mode="json")

    def broken(*args, **kwargs):
        if save_first:
            original(*args, **kwargs)
        raise OSError("synthetic disk failure")

    try:
        monkeypatch.setattr(coordinator.orchestrator, "checkpoint", broken)
        with pytest.raises(OSError):
            coordinator.select(providers[1])
        assert coordinator.runtime.model_dump(mode="json") == before
        with pytest.raises(RuntimeError, match="reload"):
            coordinator.select(providers[1])
        monkeypatch.setattr(coordinator.orchestrator, "checkpoint", original)
        recovered = coordinator.orchestrator.resume(expected_run_id="priority:test")
        assert recovered.runtime_state.priority.batch_sequence == int(save_first)
    finally:
        coordinator.orchestrator.close()


@pytest.mark.parametrize("blocked", ["permission", "budget"])
def test_current_admission_constraints_block_without_reserving_or_dispatch(tmp_path, blocked):
    coordinator, providers, permissions, resolver = setup_priority(
        tmp_path, count=1, requests=0 if blocked == "budget" else 20, with_requirement=True
    )
    try:
        if blocked == "permission":
            permissions.clear()
        selected = coordinator.select(providers[1])
        assert selected.selected_candidate_ids == ()
        assert selected.assessments[0].blocked_reasons
        assert resolver.calls == 0 and coordinator.runtime.operational_budget.charged_requests == 0
    finally:
        coordinator.orchestrator.close()


def test_rejected_uncomposed_provider_does_not_mutate_state(tmp_path):
    coordinator, _, _, _ = setup_priority(tmp_path)
    try:
        before = coordinator.runtime.model_dump(mode="json")
        with pytest.raises(RuntimeError, match="not explicitly composed"):
            coordinator.select(ExpectedUtilityPriorityProvider(max_wait_batches=10))
        assert coordinator.runtime.model_dump(mode="json") == before
    finally:
        coordinator.orchestrator.close()


def test_canonical_permission_change_during_scoring_prevents_publication(tmp_path):
    coordinator, providers, permissions, _ = setup_priority(tmp_path)
    base = providers[1]

    class ChangedProvider:
        descriptor = base.descriptor

        def score(self, facts):
            permissions.clear()
            return base.score(facts)

    try:
        with pytest.raises(RuntimeError, match="inputs changed"):
            coordinator.select(ChangedProvider())
        assert coordinator.runtime.priority.batch_sequence == 0
    finally:
        coordinator.orchestrator.close()


def test_terminal_binding_is_idempotent_and_survives_subsequent_selection(tmp_path):
    coordinator, providers, _, _ = setup_priority(tmp_path, count=1)
    try:
        selected = coordinator.select(providers[1])
        row = selected.assessments[0]
        kwargs = dict(
            root_key=row.facts.root_key,
            expected_evidence_hash=row.evidence_hash,
            disposition=CandidateDisposition.REJECTED,
            reason="trusted deterministic negative outcome",
        )
        record = coordinator.record_decision(**kwargs)
        before = coordinator.runtime.revision
        assert coordinator.record_decision(**kwargs) == record
        assert coordinator.runtime.revision == before
        terminal_selection = coordinator.select(providers[1])
        assert terminal_selection.selected_candidate_ids == ()
        assert coordinator.runtime.priority.decisions == (record,)
        candidate = next(iter(coordinator.session.candidate_state.candidates.values()))
        candidate.evidence_ids.append("new:evidence")
        changed = coordinator.select(providers[1])
        assert changed.assessments[0].reevaluation_required
        assert changed.selected_candidate_ids == (candidate.candidate_id,)
        assert candidate.disposition is CandidateDisposition.REJECTED
    finally:
        coordinator.orchestrator.close()


def test_stale_terminal_decision_is_rejected_without_publication(tmp_path):
    coordinator, providers, _, _ = setup_priority(tmp_path, count=1)
    try:
        selected = coordinator.select(providers[1])
        row = selected.assessments[0]
        candidate = next(iter(coordinator.session.candidate_state.candidates.values()))
        candidate.evidence_ids.append("new:evidence")
        with pytest.raises(ValueError, match="stale"):
            coordinator.record_decision(
                root_key=row.facts.root_key,
                expected_evidence_hash=row.evidence_hash,
                disposition=CandidateDisposition.ACCEPTED,
                reason="outdated conclusion",
            )
        assert not coordinator.runtime.priority.decisions
        assert candidate.disposition is CandidateDisposition.UNREVIEWED
    finally:
        coordinator.orchestrator.close()


def test_finish_rechecks_bound_evidence_without_another_priority_selection(tmp_path):
    from redteam_core.models.decision import ScopeCompletion
    from redteam_core.runtime.completion import assess_completion

    coordinator, providers, _, _ = setup_priority(tmp_path, count=1)
    try:
        row = coordinator.select(providers[1]).assessments[0]
        coordinator.record_decision(
            root_key=row.facts.root_key,
            expected_evidence_hash=row.evidence_hash,
            disposition=CandidateDisposition.REJECTED,
            reason="verified negative",
            user_final=True,
        )
        scope = ScopeCompletion(acquisition_complete=True, search_complete=True)
        kwargs = dict(scope=scope, priority=coordinator.runtime.priority, evaluation_time=NOW)
        assert assess_completion(coordinator.session, **kwargs).status.value == "COMPLETE"
        candidate = next(iter(coordinator.session.candidate_state.candidates.values()))
        candidate.evidence_ids.append("changed:after-decision")
        assessment = assess_completion(coordinator.session, **kwargs)
        assert assessment.status.value == "PARTIAL"
        assert assessment.stale_decision_root_keys == (row.facts.root_key,)
        assert candidate.disposition is CandidateDisposition.REJECTED
    finally:
        coordinator.orchestrator.close()
