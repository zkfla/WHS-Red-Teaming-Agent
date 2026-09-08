from datetime import UTC, datetime, timedelta

from redteam_core.investigation import (
    Observation,
    ResolutionFailureKind,
    ResolutionFailureRecord,
)
from redteam_core.models import Provenance
from redteam_core.runtime.step_loop import semantic_progress_fingerprint
from tests.unit.test_focused_context import fixture


def test_revision_only_and_duplicate_acquisition_events_are_not_progress():
    session, _candidate, _requirement, _secret = fixture()
    before = semantic_progress_fingerprint(session)
    session.knowledge.revision += 1
    assert semantic_progress_fingerprint(session) == before

    original = next(iter(session.investigation.observations.values()))
    later_provenance = Provenance(
        source_kind=original.provenance.source_kind,
        producer_id=original.provenance.producer_id,
        trust_class=original.provenance.trust_class,
        acquired_at=original.provenance.acquired_at + timedelta(seconds=1),
        content_hash=original.provenance.content_hash,
    )
    duplicate = Observation.create(
        requirement_id=original.requirement_id,
        resolver_id=original.resolver_id,
        subject_ref=original.subject_ref,
        payload=original.payload,
        provenance=later_provenance,
        validity=original.validity,
        evidence_ids=original.evidence_ids,
    )
    session.investigation.observations[duplicate.observation_id] = duplicate
    assert semantic_progress_fingerprint(session) == before


def test_repeated_same_failure_class_does_not_evade_stuck_detection():
    session, _candidate, requirement, _secret = fixture()
    before = semantic_progress_fingerprint(session)
    first = ResolutionFailureRecord.create(
        requirement_id=requirement.requirement_id,
        resolver_id="reference.resolver@1",
        kind=ResolutionFailureKind.TRANSIENT,
        detail="first wording",
        retryable=True,
        occurred_at=datetime(2026, 9, 8, tzinfo=UTC),
    )
    session.investigation.resolution_failures[first.failure_id] = first
    after_first = semantic_progress_fingerprint(session)
    assert after_first != before
    repeated = ResolutionFailureRecord.create(
        requirement_id=requirement.requirement_id,
        resolver_id="reference.resolver@1",
        kind=ResolutionFailureKind.TRANSIENT,
        detail="different wording",
        retryable=True,
        occurred_at=datetime(2026, 9, 8, tzinfo=UTC) + timedelta(seconds=1),
    )
    session.investigation.resolution_failures[repeated.failure_id] = repeated
    assert semantic_progress_fingerprint(session) == after_first
