from dataclasses import FrozenInstanceError, asdict

import pytest

from redteam_core.knowledge import CoreSession, apply_observations_atomic
from tests.helpers import catalog, observation, requirement
from tests.performance.telemetry import Collector, Stage, measured
from tests.performance.test_oracles import NOW


def failing_observer(event):
    raise RuntimeError("diagnostics unavailable")


def test_disabled_telemetry_does_not_call_clock():
    def forbidden_clock():
        raise AssertionError("disabled instrumentation invoked clock")

    assert measured(Stage.CONTEXT, lambda: 42, clock=forbidden_clock) == 42


def test_observer_failure_does_not_change_return_or_original_error():
    assert measured(Stage.CONTEXT, lambda: 42, observer=failing_observer) == 42
    original = ValueError("canonical operation error")

    def failing_operation():
        raise original

    with pytest.raises(ValueError) as caught:
        measured(Stage.COMMIT, failing_operation, observer=failing_observer)
    assert caught.value is original


def test_observer_is_bounded_numeric_and_cannot_mutate_result():
    collector = Collector(capacity=1)
    secret_result = {"raw_payload": "NEVER_EXPORT_THIS"}
    clock = iter((10, 25, 30, 45)).__next__
    for _ in range(2):
        assert (
            measured(
                Stage.CONTEXT,
                lambda: secret_result,
                observer=collector,
                input_items=2,
                clock=clock,
            )
            is secret_result
        )
    assert len(collector.events) == 1 and collector.dropped == 1
    row = collector.events[0]
    assert row.elapsed_ns == 15 and row.outcome == "OK"
    assert "NEVER_EXPORT_THIS" not in str(asdict(row))
    with pytest.raises(FrozenInstanceError):
        row.input_items = 99


def test_clock_failure_is_missing_not_zero_duration():
    collector = Collector()

    def bad_clock():
        raise RuntimeError("clock unavailable")

    assert measured(Stage.FRONTIER, lambda: 1, observer=collector, clock=bad_clock) == 1
    assert collector.events[0].elapsed_ns is None


def test_canonical_commit_identical_with_noop_enabled_or_broken_observer():
    req = requirement()
    obs = observation(req=req, value="a", acquired_at=NOW)
    snapshots = []
    for observer in (None, Collector(), failing_observer):
        session = CoreSession()
        session.investigation.requirements[req.requirement_id] = req
        measured(
            Stage.COMMIT,
            lambda session=session: apply_observations_atomic(
                session,
                (obs,),
                catalog=catalog(),
                evaluation_time=NOW,
                expected_revision=0,
            ),
            observer=observer,
            input_items=1,
        )
        snapshots.append(session.model_dump(mode="json"))
    assert snapshots[0] == snapshots[1] == snapshots[2]


def test_telemetry_configuration_rejects_unbounded_or_free_text_labels():
    with pytest.raises(ValueError):
        Collector(capacity=0)
    with pytest.raises(ValueError):
        measured("raw prompt", lambda: None)
