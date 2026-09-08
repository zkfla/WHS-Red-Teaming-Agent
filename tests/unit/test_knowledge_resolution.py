from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import BaseModel

from redteam_core.investigation import (
    EvidenceResolutionError,
    Observation,
    ObservationValidity,
    ResolutionFailureKind,
    ResolverDescriptor,
    ResolverOutput,
    ResolverRuntimeRegistry,
    resolve_and_record,
    resolve_to_observation,
)
from redteam_core.knowledge import (
    CoreSession,
    KnowledgeSlotStatus,
    SemanticCommitConflict,
    apply_observation,
    apply_observations_atomic,
    refresh_freshness,
)
from redteam_core.models.provenance import TrustClass
from redteam_core.runtime import FixedClock
from tests.helpers import (
    REQUIREMENT_TYPE,
    AlternatePayload,
    SamplePayload,
    catalog,
    observation,
    requirement,
)

NOW = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)


@dataclass
class FakeResolver:
    descriptor: ResolverDescriptor
    payload: BaseModel | None = None
    error: EvidenceResolutionError | None = None

    async def resolve(self, requirement):
        if self.error is not None:
            raise self.error
        assert self.payload is not None
        return ResolverOutput(payload=self.payload)


def _descriptor(
    resolver_id: str,
    *,
    trust_rank: int = 50,
    cost_rank: int = 50,
    output_trust_class: TrustClass = TrustClass.OBSERVED,
    required_permissions: tuple[str, ...] = (),
    max_calls: int = 1,
):
    return ResolverDescriptor(
        resolver_id=resolver_id,
        provider_id="reference.local",
        supported_requirement_type_ids=frozenset({REQUIREMENT_TYPE}),
        trust_rank=trust_rank,
        cost_rank=cost_rank,
        output_trust_class=output_trust_class,
        required_permissions=required_permissions,
        max_calls=max_calls,
    )


def _session_with_requirement(req):
    session = CoreSession()
    session.investigation.requirements[req.requirement_id] = req
    return session


def test_multiple_resolvers_are_ranked_deterministically_not_by_registration_order():
    req = requirement()
    alpha = FakeResolver(
        _descriptor("reference.resolver.alpha@1", trust_rank=80, cost_rank=10),
        SamplePayload(value="a"),
    )
    beta = FakeResolver(
        _descriptor("reference.resolver.beta@1", trust_rank=90, cost_rank=90),
        SamplePayload(value="b"),
    )
    first = ResolverRuntimeRegistry()
    first.register(alpha)
    first.register(beta)
    first.freeze()
    second = ResolverRuntimeRegistry()
    second.register(beta)
    second.register(alpha)
    second.freeze()
    assert first.select(req).descriptor.resolver_id == "reference.resolver.beta@1"
    assert second.select(req).descriptor.resolver_id == "reference.resolver.beta@1"


def test_requirement_identity_binds_freshness_contract():
    plain = requirement()
    fresh = requirement(max_age_seconds=60)
    snap = requirement(required_snapshot_ref="snapshot:one")
    assert plain.requirement_id != fresh.requirement_id
    assert plain.requirement_id != snap.requirement_id


@pytest.mark.asyncio
async def test_resolver_returns_typed_requirement_bound_observation():
    req = requirement()
    registry = ResolverRuntimeRegistry()
    registry.register(
        FakeResolver(
            _descriptor("reference.resolver.alpha@1"),
            SamplePayload(value="observed"),
        )
    )
    registry.freeze()
    obs = await resolve_to_observation(
        requirement=req,
        resolver_registry=registry,
        catalog=catalog(),
        clock=FixedClock(NOW),
    )
    assert obs.requirement_id == req.requirement_id
    assert obs.payload.type_id == "reference.observation.value@1"
    assert obs.provenance.trust_class is TrustClass.OBSERVED


@pytest.mark.asyncio
async def test_invalid_resolver_payload_is_failure_not_security_truth():
    req = requirement()
    session = _session_with_requirement(req)
    registry = ResolverRuntimeRegistry()
    registry.register(
        FakeResolver(
            _descriptor("reference.resolver.alpha@1"),
            AlternatePayload(count=3),
        )
    )
    registry.freeze()
    attempt = await resolve_and_record(
        session=session,
        requirement=req,
        resolver_registry=registry,
        catalog=catalog(),
        clock=FixedClock(NOW),
    )
    assert attempt.failure is not None
    assert attempt.failure.kind is ResolutionFailureKind.INVALID_PAYLOAD
    assert session.knowledge.revision == 0
    assert session.knowledge.slots == {}
    assert session.investigation.observations == {}


@pytest.mark.asyncio
async def test_forbidden_resolution_failure_only_updates_failure_ledger():
    req = requirement()
    session = _session_with_requirement(req)
    registry = ResolverRuntimeRegistry()
    registry.register(
        FakeResolver(
            _descriptor("reference.resolver.alpha@1"),
            error=EvidenceResolutionError(
                ResolutionFailureKind.FORBIDDEN,
                "provider denied access",
            ),
        )
    )
    registry.freeze()
    attempt = await resolve_and_record(
        session=session,
        requirement=req,
        resolver_registry=registry,
        catalog=catalog(),
        clock=FixedClock(NOW),
    )
    assert attempt.failure and attempt.failure.kind is ResolutionFailureKind.FORBIDDEN
    assert len(session.investigation.resolution_failures) == 1
    assert session.knowledge.revision == 0
    assert session.knowledge.slots == {}


@pytest.mark.asyncio
async def test_missing_resolver_records_unsupported_without_truth():
    req = requirement()
    session = _session_with_requirement(req)
    registry = ResolverRuntimeRegistry()
    registry.freeze()
    attempt = await resolve_and_record(
        session=session,
        requirement=req,
        resolver_registry=registry,
        catalog=catalog(),
        clock=FixedClock(NOW),
    )
    assert attempt.failure and attempt.failure.kind is ResolutionFailureKind.UNSUPPORTED
    assert attempt.failure.resolver_id is None
    assert session.knowledge.revision == 0
    assert session.knowledge.slots == {}


@pytest.mark.asyncio
async def test_resolver_permission_requirement_fails_closed_before_call():
    req = requirement()
    session = _session_with_requirement(req)
    resolver = FakeResolver(
        _descriptor(
            "reference.resolver.permissioned@1",
            required_permissions=("reference.read",),
        ),
        SamplePayload(value="secret"),
    )
    registry = ResolverRuntimeRegistry()
    registry.register(resolver)
    registry.freeze()
    attempt = await resolve_and_record(
        session=session,
        requirement=req,
        resolver_registry=registry,
        catalog=catalog(),
        clock=FixedClock(NOW),
    )
    assert attempt.failure and attempt.failure.kind is ResolutionFailureKind.AUTH_REQUIRED
    assert registry.call_count("reference.resolver.permissioned@1") == 0
    assert session.knowledge.revision == 0


@pytest.mark.asyncio
async def test_resolver_call_budget_is_enforced_deterministically():
    req = requirement()
    resolver = FakeResolver(
        _descriptor("reference.resolver.budgeted@1", max_calls=1),
        SamplePayload(value="one"),
    )
    registry = ResolverRuntimeRegistry()
    registry.register(resolver)
    registry.freeze()
    first = await resolve_to_observation(
        requirement=req,
        resolver_registry=registry,
        catalog=catalog(),
        clock=FixedClock(NOW),
    )
    assert first.payload.type_id == "reference.observation.value@1"
    session = _session_with_requirement(req)
    second = await resolve_and_record(
        session=session,
        requirement=req,
        resolver_registry=registry,
        catalog=catalog(),
        clock=FixedClock(NOW),
    )
    assert second.failure and second.failure.kind is ResolutionFailureKind.BUDGET_EXHAUSTED
    assert registry.call_count("reference.resolver.budgeted@1") == 1


@pytest.mark.asyncio
async def test_next_eligible_resolver_is_selected_after_higher_rank_budget_is_consumed():
    req = requirement()
    primary = FakeResolver(
        _descriptor("reference.resolver.primary@1", trust_rank=90, max_calls=1),
        SamplePayload(value="primary"),
    )
    fallback = FakeResolver(
        _descriptor("reference.resolver.fallback@1", trust_rank=80, max_calls=1),
        SamplePayload(value="fallback"),
    )
    registry = ResolverRuntimeRegistry()
    registry.register(fallback)
    registry.register(primary)
    registry.freeze()
    first = await resolve_to_observation(
        requirement=req,
        resolver_registry=registry,
        catalog=catalog(),
        clock=FixedClock(NOW),
    )
    second = await resolve_to_observation(
        requirement=req,
        resolver_registry=registry,
        catalog=catalog(),
        clock=FixedClock(NOW),
    )
    assert first.resolver_id == "reference.resolver.primary@1"
    assert second.resolver_id == "reference.resolver.fallback@1"


def test_duplicate_exact_observation_is_idempotent_revision_plus_zero():
    req = requirement()
    session = _session_with_requirement(req)
    obs = observation(req=req, value="one", acquired_at=NOW)
    first = apply_observation(
        session,
        obs,
        catalog=catalog(),
        evaluation_time=NOW,
        expected_revision=0,
    )
    second = apply_observation(
        session,
        obs,
        catalog=catalog(),
        evaluation_time=NOW,
        expected_revision=1,
    )
    assert first.semantic_changed and first.revision == 1
    assert not second.semantic_changed and second.revision == 1
    assert second.duplicate_observation_ids == (obs.observation_id,)


def test_conflicting_authoritative_observations_are_preserved_fail_closed():
    req = requirement()
    session = _session_with_requirement(req)
    one = observation(req=req, value="one", acquired_at=NOW, resolver_id="reference.resolver.one@1")
    two = observation(req=req, value="two", acquired_at=NOW, resolver_id="reference.resolver.two@1")
    result = apply_observations_atomic(
        session,
        (one, two),
        catalog=catalog(),
        evaluation_time=NOW,
        expected_revision=0,
    )
    slot = session.knowledge.slots[req.requirement_id]
    assert result.revision == 1
    assert slot.status is KnowledgeSlotStatus.CONFLICTING
    assert slot.effective_payload_hash is None
    assert set(slot.authoritative_observation_ids) == {one.observation_id, two.observation_id}


def test_inferred_conflict_cannot_override_authoritative_observation():
    req = requirement()
    session = _session_with_requirement(req)
    observed = observation(req=req, value="truth", acquired_at=NOW)
    inferred = observation(
        req=req,
        value="guess",
        acquired_at=NOW,
        resolver_id="reference.resolver.inferred@1",
        trust_class=TrustClass.INFERRED,
    )
    apply_observations_atomic(
        session,
        (inferred, observed),
        catalog=catalog(),
        evaluation_time=NOW,
        expected_revision=0,
    )
    slot = session.knowledge.slots[req.requirement_id]
    assert slot.status is KnowledgeSlotStatus.CONSISTENT
    assert slot.effective_payload_hash == observed.payload.content_hash
    assert slot.inferred_observation_ids == (inferred.observation_id,)


def test_inferred_only_observation_remains_unknown():
    req = requirement()
    session = _session_with_requirement(req)
    inferred = observation(
        req=req,
        value="guess",
        acquired_at=NOW,
        resolver_id="reference.resolver.inferred@1",
        trust_class=TrustClass.INFERRED,
    )
    apply_observation(
        session,
        inferred,
        catalog=catalog(),
        evaluation_time=NOW,
        expected_revision=0,
    )
    slot = session.knowledge.slots[req.requirement_id]
    assert slot.status is KnowledgeSlotStatus.UNKNOWN
    assert slot.effective_payload_hash is None


def test_batch_commit_is_order_independent_and_single_revision():
    req = requirement()
    one = observation(req=req, value="one", acquired_at=NOW, resolver_id="reference.resolver.one@1")
    two = observation(req=req, value="two", acquired_at=NOW, resolver_id="reference.resolver.two@1")
    left = _session_with_requirement(req)
    right = _session_with_requirement(req)
    left_result = apply_observations_atomic(
        left,
        (one, two),
        catalog=catalog(),
        evaluation_time=NOW,
        expected_revision=0,
    )
    right_result = apply_observations_atomic(
        right,
        (two, one),
        catalog=catalog(),
        evaluation_time=NOW,
        expected_revision=0,
    )
    assert left.knowledge == right.knowledge
    assert left.investigation == right.investigation
    assert left_result.revision == right_result.revision == 1


def test_stale_observation_does_not_become_current_truth_and_refresh_expires_old_truth():
    req = requirement(max_age_seconds=60)
    session = _session_with_requirement(req)
    obs = observation(req=req, value="current", acquired_at=NOW)
    apply_observation(
        session,
        obs,
        catalog=catalog(),
        evaluation_time=NOW + timedelta(seconds=30),
        expected_revision=0,
    )
    assert session.knowledge.slots[req.requirement_id].status is KnowledgeSlotStatus.CONSISTENT
    result = refresh_freshness(
        session,
        evaluation_time=NOW + timedelta(seconds=61),
        expected_revision=1,
    )
    slot = session.knowledge.slots[req.requirement_id]
    assert result.semantic_changed and result.revision == 2
    assert slot.status is KnowledgeSlotStatus.UNKNOWN
    assert slot.stale_observation_ids == (obs.observation_id,)


def test_stale_first_observation_remains_unknown():
    req = requirement(max_age_seconds=10)
    session = _session_with_requirement(req)
    obs = observation(req=req, value="old", acquired_at=NOW)
    apply_observation(
        session,
        obs,
        catalog=catalog(),
        evaluation_time=NOW + timedelta(seconds=20),
        expected_revision=0,
    )
    slot = session.knowledge.slots[req.requirement_id]
    assert slot.status is KnowledgeSlotStatus.UNKNOWN
    assert slot.effective_payload_hash is None


def test_snapshot_mismatch_is_not_current_truth():
    req = requirement(required_snapshot_ref="snapshot:expected")
    session = _session_with_requirement(req)
    obs = observation(
        req=req,
        value="wrong-snapshot",
        acquired_at=NOW,
        validity=ObservationValidity(snapshot_ref="snapshot:other"),
    )
    apply_observation(
        session,
        obs,
        catalog=catalog(),
        evaluation_time=NOW,
        expected_revision=0,
    )
    slot = session.knowledge.slots[req.requirement_id]
    assert slot.status is KnowledgeSlotStatus.UNKNOWN
    assert slot.stale_observation_ids == (obs.observation_id,)


def test_not_after_expiry_is_not_current_truth():
    req = requirement()
    session = _session_with_requirement(req)
    obs = observation(
        req=req,
        value="expired",
        acquired_at=NOW,
        validity=ObservationValidity(not_after=NOW + timedelta(seconds=5)),
    )
    apply_observation(
        session,
        obs,
        catalog=catalog(),
        evaluation_time=NOW + timedelta(seconds=6),
        expected_revision=0,
    )
    assert session.knowledge.slots[req.requirement_id].status is KnowledgeSlotStatus.UNKNOWN


def test_expected_revision_conflict_rejects_without_mutation():
    req = requirement()
    session = _session_with_requirement(req)
    obs = observation(req=req, value="one", acquired_at=NOW)
    before = session.model_copy(deep=True)
    with pytest.raises(SemanticCommitConflict):
        apply_observation(
            session,
            obs,
            catalog=catalog(),
            evaluation_time=NOW,
            expected_revision=9,
        )
    assert session == before


def test_atomic_batch_rolls_back_if_any_observation_is_invalid():
    req = requirement()
    session = _session_with_requirement(req)
    valid = observation(req=req, value="one", acquired_at=NOW)
    invalid = Observation.create(
        requirement_id=req.requirement_id,
        resolver_id=valid.resolver_id,
        subject_ref="subject:other",
        payload=valid.payload,
        provenance=valid.provenance,
    )
    with pytest.raises(ValueError, match="subject"):
        apply_observations_atomic(
            session,
            (valid, invalid),
            catalog=catalog(),
            evaluation_time=NOW,
            expected_revision=0,
        )
    assert session.investigation.observations == {}
    assert session.knowledge.revision == 0


def test_same_payload_from_multiple_authoritative_resolvers_remains_consistent():
    req = requirement()
    session = _session_with_requirement(req)
    one = observation(
        req=req,
        value="same",
        acquired_at=NOW,
        resolver_id="reference.resolver.one@1",
    )
    two = observation(
        req=req,
        value="same",
        acquired_at=NOW,
        resolver_id="reference.resolver.two@1",
    )
    apply_observations_atomic(
        session,
        (one, two),
        catalog=catalog(),
        evaluation_time=NOW,
        expected_revision=0,
    )
    slot = session.knowledge.slots[req.requirement_id]
    assert slot.status is KnowledgeSlotStatus.CONSISTENT
    assert slot.authoritative_payload_hashes == (one.payload.content_hash,)
