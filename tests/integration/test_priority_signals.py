from datetime import timedelta

import pytest

from redteam_core.knowledge.commit import apply_observations_atomic
from redteam_core.models.requirements import ResolutionFailureKind, ResolutionFailureRecord
from tests.helpers import observation
from tests.integration.test_durable_priority import setup_priority
from tests.unit.test_priority import NOW


def test_receipt_capacity_is_an_admission_budget_not_a_dispatch_surprise(tmp_path, monkeypatch):
    from redteam_core.investigation import priority

    coordinator, providers, _, resolver = setup_priority(tmp_path, count=1, with_requirement=True)
    monkeypatch.setattr(priority, "MAX_RESOLUTION_RECEIPT_LEDGER_BYTES", 4096)
    try:
        selection = coordinator.select(providers[1])
        assert selection.selected_candidate_ids == ()
        assert selection.assessments[0].blocked_reasons == ("BUDGET_UNAVAILABLE",)
        assert resolver.calls == 0
    finally:
        coordinator.orchestrator.close()


def test_shared_requirement_and_failure_history_are_canonical_and_durable(tmp_path):
    coordinator, providers, _, resolver = setup_priority(tmp_path, count=2, with_requirement=True)
    try:
        first = coordinator.select(providers[1])
        assert all(row.facts.shared_unresolved_roots == 1 for row in first.assessments)
        assert all(row.facts.resolvable_count == 1 for row in first.assessments)
        assert all(row.facts.cost_bucket == 2 for row in first.assessments)
        req = next(iter(coordinator.session.investigation.requirements))
        for index in range(2):
            failure = ResolutionFailureRecord.create(
                requirement_id=req,
                resolver_id=resolver.descriptor.resolver_id,
                kind=ResolutionFailureKind.TRANSIENT,
                detail="synthetic known failure",
                retryable=False,
                occurred_at=NOW + timedelta(seconds=index),
            )
            coordinator.session.investigation.resolution_failures[failure.failure_id] = failure
        second = coordinator.select(providers[1])
        assert all(row.facts.failure_count == 2 for row in second.assessments)
        assert all(
            next(c.value for c in row.score.components if c.name == "repeated_failures") == -10
            for row in second.assessments
        )
        coordinator.session.investigation.resolution_failures.clear()
        third = coordinator.select(providers[0])
        assert all(row.facts.failure_count == 2 for row in third.assessments)
        assert resolver.calls == 0
    finally:
        coordinator.orchestrator.close()


@pytest.mark.asyncio
async def test_distinct_failed_dispatches_count_even_when_semantic_failure_is_deduplicated(
    tmp_path,
):
    from redteam_core.investigation.resolution import EvidenceResolutionError, ResolverOutput
    from redteam_core.runtime.resolution_commit import commit_resolution_receipts
    from redteam_core.runtime.resolver_dispatch import DurableResolverDispatcher
    from tests.helpers import AlternatePayload

    coordinator, providers, permissions, resolver = setup_priority(
        tmp_path, count=1, with_requirement=True
    )
    dispatcher = DurableResolverDispatcher(
        session=coordinator.session,
        runtime=coordinator.runtime,
        orchestrator=coordinator.orchestrator,
        registry=coordinator.registry,
        permissions=lambda: frozenset(permissions),
    )

    async def invalid_response(_requirement):
        return ResolverOutput(payload=AlternatePayload(count=0), actual_tokens=0)

    resolver.resolve = invalid_response
    requirement_id = next(iter(coordinator.session.investigation.requirements))
    try:
        for index in range(2):
            attempt = f"invalid:{index}"
            with pytest.raises(EvidenceResolutionError):
                await dispatcher.resolve(requirement_id, attempt_id=attempt)
            commit_resolution_receipts(
                dispatcher,
                attempt_ids=(attempt,),
                expected_revision=coordinator.session.knowledge.revision,
            )
            selected = coordinator.select(providers[1])
            assert selected.assessments[0].facts.failure_count == index + 1
        assert len(coordinator.session.investigation.resolution_failures) == 1
    finally:
        coordinator.orchestrator.close()


def test_fresh_negative_and_conflicting_evidence_are_opportunities_not_positive_claims(tmp_path):
    coordinator, providers, permissions, resolver = setup_priority(
        tmp_path, count=1, with_requirement=True
    )
    try:
        req = next(iter(coordinator.session.investigation.requirements.values()))
        permissions.clear()
        apply_observations_atomic(
            coordinator.session,
            observations=(observation(req=req, value="negative", acquired_at=NOW),),
            catalog=coordinator.orchestrator.catalog,
            evaluation_time=NOW,
            expected_revision=coordinator.session.knowledge.revision,
        )
        negative = coordinator.select(providers[1]).assessments[0]
        assert negative.eligible and negative.facts.fresh_count == 1
        assert negative.facts.resolvable_count == 0 and negative.facts.conflict_count == 0
        apply_observations_atomic(
            coordinator.session,
            observations=(
                observation(
                    req=req, value="different", acquired_at=NOW, resolver_id="reference.other@1"
                ),
            ),
            catalog=coordinator.orchestrator.catalog,
            evaluation_time=NOW,
            expected_revision=coordinator.session.knowledge.revision,
        )
        conflict = coordinator.select(providers[1]).assessments[0]
        assert conflict.eligible and conflict.facts.conflict_count == 1
        assert coordinator.session.knowledge.slots[req.requirement_id].status.value == "CONFLICTING"
        assert not coordinator.runtime.priority.decisions and resolver.calls == 0
    finally:
        coordinator.orchestrator.close()
