"""F02: observed-expiry detection, coalescing, envelopes, and pause gating."""

# Pure unit tests with explicit fakes and a controllable clock: no live
# provider, target, model, or database. Time-driven re-judgment uses only
# originals-grounded observed conditions; unknown conditions get no invented TTL.

from datetime import datetime, timedelta, timezone

from redteam.core.contracts.events import EventType
from redteam.core.enums import MissionStatus
from redteam.orchestration.coordination import StateConflict
from redteam.orchestration.evidence_validity import (
    ExpiryWatch,
    ValidityChange,
    ValidityProjection,
    _projection_fingerprint,
    detect_validity_changes,
    emit_allowed,
    next_expiry,
    queue_validity_change,
    validity_change_envelope,
)


def projection(evidence_id, status, *, valid_until=None, bindings=()):
    return ValidityProjection(
        evidence_id=evidence_id, status=status,
        binding_ids=list(bindings), valid_until=valid_until,
    )


def test_next_expiry_ignores_unknown_conditions():
    now = datetime.now(timezone.utc)
    assert next_expiry([], now=now) is None
    assert next_expiry([projection("e1", "unbound"), projection("e2", "valid")],
                       now=now) is None


def test_next_expiry_returns_earliest_future_deadline():
    now = datetime.now(timezone.utc)
    soon = now + timedelta(seconds=5)
    later = now + timedelta(seconds=60)
    current = [projection("e1", "valid", valid_until=later, bindings=["b1"]),
               projection("e2", "valid", valid_until=soon, bindings=["b2"])]
    assert next_expiry(current, now=now) == soon


def test_next_expiry_with_only_past_deadlines_returns_none():
    now = datetime.now(timezone.utc)
    past = now - timedelta(seconds=5)
    current = [projection("e1", "stale", valid_until=past, bindings=["b1"])]
    assert next_expiry(current, now=now) is None


def test_newly_observed_evidence_is_silent_not_a_change():
    now = datetime.now(timezone.utc)
    current = [projection("e1", "stale",
                           valid_until=now - timedelta(seconds=1), bindings=["b1"])]
    assert detect_validity_changes({}, current, now=now) == []


def test_unchanged_projection_produces_no_change():
    now = datetime.now(timezone.utc)
    current = [projection("e1", "valid",
                           valid_until=now + timedelta(seconds=30), bindings=["b1"])]
    previous = {"e1": _projection_fingerprint(current[0])}
    assert detect_validity_changes(previous, current, now=now) == []


def test_time_passage_over_observed_expiry_reports_reason():
    now = datetime.now(timezone.utc)
    deadline = now + timedelta(seconds=30)
    before = [projection("e1", "valid", valid_until=deadline, bindings=["b1"])]
    previous = {"e1": _projection_fingerprint(before[0])}
    after = [projection("e1", "stale", valid_until=deadline, bindings=["b1"])]
    (change,) = detect_validity_changes(previous, after,
                                        now=deadline + timedelta(seconds=1))
    assert change.evidence_id == "e1"
    assert (change.old_status, change.new_status) == ("valid", "stale")
    assert "observed expiry passed" in change.reason
    assert "past observation kept" in change.reason
    assert change.evidence_refs == ["e1", "b1"]


def test_condition_change_without_deadline_reports_transition():
    now = datetime.now(timezone.utc)
    before = [projection("e1", "valid", bindings=["b1"])]
    previous = {"e1": _projection_fingerprint(before[0])}
    after = [projection("e1", "invalid", bindings=["b1"])]
    (change,) = detect_validity_changes(previous, after, now=now)
    assert (change.old_status, change.new_status) == ("valid", "invalid")
    assert "changed from valid to invalid" in change.reason


def test_binding_set_change_that_keeps_reuse_valid_is_quiet():
    now = datetime.now(timezone.utc)
    before = [projection("e1", "valid", bindings=["b1"])]
    previous = {"e1": _projection_fingerprint(before[0])}
    after = [projection("e1", "valid", bindings=["b1", "b2"])]
    assert detect_validity_changes(previous, after, now=now) == []


def test_watch_coalesces_duplicate_notifications_for_one_change():
    now = datetime.now(timezone.utc)
    deadline = now + timedelta(seconds=30)
    watch = ExpiryWatch.from_projections(
        [projection("e1", "valid", valid_until=deadline, bindings=["b1"])])
    assert watch.tracked == ("e1",)
    stale = [projection("e1", "stale", valid_until=deadline, bindings=["b1"])]
    changes = watch.observe(stale, now=deadline + timedelta(seconds=1))
    assert [change.new_status for change in changes] == ["stale"]
    assert watch.observe(stale, now=deadline + timedelta(seconds=60)) == []
    assert watch.observe(stale, now=deadline + timedelta(seconds=3600)) == []


def test_watch_restart_from_originals_stays_silent():
    now = datetime.now(timezone.utc)
    stale = [projection("e1", "stale",
                          valid_until=now - timedelta(seconds=1), bindings=["b1"])]
    restarted = ExpiryWatch.from_projections(stale)
    assert restarted.observe(stale, now=now) == []


def test_change_envelope_is_deterministic_and_carries_refs():
    change = ValidityChange(
        evidence_id="e1", old_status="valid", new_status="stale",
        reason="observed expiry passed at T; past observation kept, current reuse unproven",
        binding_ids=["b1"], evidence_refs=["e1", "b1"],
    )
    first = validity_change_envelope(run_id="run_1", engagement_id="eng_1",
                                     change=change)
    second = validity_change_envelope(run_id="run_1", engagement_id="eng_1",
                                      change=change)
    assert first.event_id == second.event_id
    assert first.event_type == EventType.EVIDENCE_VALIDITY_CHANGED
    assert first.payload["evidence_refs"] == ["e1", "b1"]
    assert "past observation kept" in first.payload["reason"]


class _FakeTx:
    """Mimics CentralTransaction.queue identity semantics for unit tests."""

    def __init__(self):
        self.queued = {}

    def queue(self, envelope):
        key = str(envelope.event_id)
        payload = envelope.model_dump(mode="json")
        old = self.queued.get(key)
        if old is not None:
            if old != payload:
                raise StateConflict("event_identity_conflict")
            return
        self.queued[key] = payload


def _change():
    return ValidityChange(
        evidence_id="e1", old_status="valid", new_status="stale",
        reason="observed expiry passed at T; past observation kept, current reuse unproven",
        binding_ids=["b1"], evidence_refs=["e1", "b1"],
    )


def test_queue_is_idempotent_for_the_same_change():
    change = _change()
    first = validity_change_envelope(run_id="run_1", engagement_id="eng_1",
                                     change=change)
    tx = _FakeTx()
    assert queue_validity_change(tx, first) is True
    assert queue_validity_change(tx, first) is True
    assert len(tx.queued) == 1


def test_queue_coalesces_same_change_with_fresh_timestamp():
    change = _change()
    tx = _FakeTx()
    assert queue_validity_change(tx, validity_change_envelope(
        run_id="run_1", engagement_id="eng_1", change=change)) is True
    assert queue_validity_change(tx, validity_change_envelope(
        run_id="run_1", engagement_id="eng_1", change=change)) is True
    assert len(tx.queued) == 1


def test_queue_propagates_unrelated_conflicts():
    change = _change()
    envelope = validity_change_envelope(run_id="run_1", engagement_id="eng_1",
                                        change=change)

    class _HostileTx(_FakeTx):
        def queue(self, envelope):
            raise StateConflict("outbox_binding_mismatched")

    try:
        queue_validity_change(_HostileTx(), envelope)
    except StateConflict:
        return
    raise AssertionError("unrelated conflict swallowed")


def test_emit_allowed_only_while_active():
    assert emit_allowed(MissionStatus.ACTIVE) is True
    for status in (MissionStatus.PAUSED, MissionStatus.CANCELLED,
                   MissionStatus.COMPLETED, MissionStatus.DRAFT,
                   MissionStatus.BLOCKED, MissionStatus.FAILED):
        assert emit_allowed(status) is False


def test_pending_peek_does_not_acknowledge():
    now = datetime.now(timezone.utc)
    deadline = now + timedelta(seconds=30)
    watch = ExpiryWatch.from_projections([
        projection("e1", "valid", valid_until=deadline, bindings=["b1"])])
    stale = [projection("e1", "stale", valid_until=deadline, bindings=["b1"])]
    moment = deadline + timedelta(seconds=1)
    assert len(watch.pending_changes(stale, now=moment)) == 1
    assert len(watch.pending_changes(stale, now=moment)) == 1
    watch.advance(stale)
    assert watch.pending_changes(stale, now=moment) == []
    assert watch.observe(stale, now=moment) == []


def test_unacknowledged_change_is_never_dropped():
    now = datetime.now(timezone.utc)
    deadline = now + timedelta(seconds=30)
    watch = ExpiryWatch.from_projections([
        projection("e1", "valid", valid_until=deadline, bindings=["b1"])])
    stale = [projection("e1", "stale", valid_until=deadline, bindings=["b1"])]
    moment = deadline + timedelta(seconds=1)
    first = watch.pending_changes(stale, now=moment)
    assert len(first) == 1
    second = watch.pending_changes(stale, now=moment)
    assert [change.reason for change in second] == [change.reason for change in first]
