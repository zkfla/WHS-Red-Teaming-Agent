from datetime import timedelta

import pytest

from redteam_core.knowledge import (
    SemanticCommitConflict,
    apply_observations_atomic,
    refresh_freshness,
)
from redteam_core.knowledge.recompute import effective_payload_envelope
from tests.performance.workloads import NOW, knowledge_input


@pytest.mark.parametrize("cycles", [1, 10])
def test_full_and_indexed_commit_refresh_and_payload_are_identical(cycles):
    results = []
    for mode in ("full_scan", "indexed"):
        session, cat, observations = knowledge_input(100)
        receipts = []
        for cycle in range(cycles):
            batch = observations[cycle * 100 // cycles : (cycle + 1) * 100 // cycles]
            for _ in range(2):  # duplicate receipt must remain idempotent
                receipts.append(
                    apply_observations_atomic(
                        session,
                        batch,
                        catalog=cat,
                        evaluation_time=NOW,
                        expected_revision=session.knowledge.revision,
                        lookup_mode=mode,
                    )
                )
        for req in session.investigation.requirements:
            assert effective_payload_envelope(session, req, lookup_mode="indexed") == (
                effective_payload_envelope(session, req, lookup_mode="full_scan")
            )
        receipts.append(
            refresh_freshness(
                session,
                evaluation_time=NOW + timedelta(seconds=60),
                expected_revision=session.knowledge.revision,
                lookup_mode=mode,
            )
        )
        results.append((session, receipts))
    assert results[0] == results[1]


class CountingLedger(dict):
    scans = 0

    def values(self):
        type(self).scans += 1
        return super().values()


@pytest.mark.parametrize("mode,expected_scans", [("full_scan", 10), ("indexed", 1)])
def test_freshness_global_scan_count(mode, expected_scans):
    session, cat, observations = knowledge_input(100)
    apply_observations_atomic(
        session, observations, catalog=cat, evaluation_time=NOW, expected_revision=0
    )
    # Test instrumentation only: preserve ledger behavior, count each .values() call.
    object.__setattr__(
        session.investigation, "observations", CountingLedger(session.investigation.observations)
    )
    CountingLedger.scans = 0
    refresh_freshness(
        session, evaluation_time=NOW, expected_revision=session.knowledge.revision, lookup_mode=mode
    )
    assert CountingLedger.scans == expected_scans


def test_index_is_rebuilt_after_public_ledger_mutation():
    session, cat, observations = knowledge_input(20)
    apply_observations_atomic(
        session,
        observations,
        catalog=cat,
        evaluation_time=NOW,
        expected_revision=0,
        lookup_mode="indexed",
    )
    session.investigation.observations.clear()
    refresh_freshness(
        session,
        evaluation_time=NOW,
        expected_revision=session.knowledge.revision,
        lookup_mode="indexed",
    )
    assert all(slot.status.value == "UNKNOWN" for slot in session.knowledge.slots.values())


def test_invalid_mode_does_not_commit():
    session, cat, observations = knowledge_input(10)
    before = session.model_copy(deep=True)
    with pytest.raises(ValueError):
        apply_observations_atomic(
            session,
            observations,
            catalog=cat,
            evaluation_time=NOW,
            expected_revision=0,
            lookup_mode="typo",
        )
    assert session == before


@pytest.mark.parametrize("mode", ["full_scan", "indexed"])
@pytest.mark.parametrize("failure", ["unknown", "collision", "future", "revision", "recomputer"])
def test_failed_batch_preserves_all_authoritative_ledgers(mode, failure):
    session, cat, observations = knowledge_input(20)
    batch = observations

    class BrokenRecomputer:
        recomputer_id = "reference.recomputer.failure@1"

        def recompute(self, view, catalog):
            view.investigation.observations.clear()
            raise RuntimeError("recompute failed")

    if failure == "unknown":
        session.investigation.requirements.pop(observations[-1].requirement_id)
    elif failure == "collision":
        session.investigation.observations[observations[-1].observation_id] = observations[
            -1
        ].model_copy(update={"subject_ref": "different"})
    before = session.model_copy(deep=True)
    expected_exception = (
        RuntimeError
        if failure == "recomputer"
        else SemanticCommitConflict
        if failure == "revision"
        else ValueError
    )
    with pytest.raises(expected_exception):
        apply_observations_atomic(
            session,
            batch,
            catalog=cat,
            evaluation_time=NOW - timedelta(days=1) if failure == "future" else NOW,
            expected_revision=999 if failure == "revision" else 0,
            lookup_mode=mode,
            recomputers=(BrokenRecomputer(),) if failure == "recomputer" else (),
        )
    assert session == before


def test_indexed_payload_uses_direct_ids_not_global_scan():
    session, cat, observations = knowledge_input(100)
    apply_observations_atomic(
        session, observations, catalog=cat, evaluation_time=NOW, expected_revision=0
    )
    object.__setattr__(
        session.investigation, "observations", CountingLedger(session.investigation.observations)
    )
    CountingLedger.scans = 0
    for req in session.investigation.requirements:
        assert effective_payload_envelope(session, req, lookup_mode="indexed") is not None
    assert CountingLedger.scans == 0
