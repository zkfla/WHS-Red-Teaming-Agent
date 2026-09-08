import pytest

from redteam_core.models.operational_budget import (
    OperationalBudgetLimits,
    OperationalBudgetState,
    RequestReservation,
    ScopeRequestLimit,
)
from redteam_core.models.operational_budget import (
    ReservationStatus as Status,
)
from redteam_core.runtime.operational_budget import (
    OperationalBudgetExceeded,
    recover_reservations,
    reserve_request,
    transition_request,
)
from tests.performance.oracles import scheduler_oracle


def state(**limits):
    return OperationalBudgetState(
        run_id="run:test",
        composition_hash="sha256:" + "a" * 64,
        limits=OperationalBudgetLimits(**limits),
    )


def request(attempt="a", **changes):
    return RequestReservation(
        **{
            "attempt_id": attempt,
            "provider_id": "provider:one",
            "resolver_id": "resolver:one",
            "reserved_tokens": 10,
            "token_bound_guaranteed": True,
            **changes,
        }
    )


@pytest.mark.parametrize("cut", [0, 1, 2])
def test_restart_matches_independent_request_oracle(cut):
    current = state(max_requests=1)
    events = (("RESERVE", "a"), ("DISPATCH", "a"))[:cut]
    if cut:
        current = reserve_request(current, request(), expected_revision=current.revision)
    if cut == 2:
        current = transition_request(
            current, "a", Status.DISPATCHED, expected_revision=current.revision
        )
    current = OperationalBudgetState.model_validate_json(current.model_dump_json())
    restored = recover_reservations(current, expected_revision=current.revision)
    expected = scheduler_oracle((*events, ("RESTART", "")), 1)
    assert 1 - restored.charged_requests == expected["remaining"]
    assert recover_reservations(restored, expected_revision=restored.revision) == restored
    if cut == 2:
        assert restored.reservations[0].status is Status.UNCERTAIN
        assert restored.active_requests == 1


@pytest.mark.parametrize(
    "limits",
    [
        {"max_requests": 0},
        {"max_tokens": 9},
        {"provider_limits": (ScopeRequestLimit(scope_id="provider:one", max_requests=0),)},
        {"resolver_limits": (ScopeRequestLimit(scope_id="resolver:one", max_requests=0),)},
    ],
)
def test_scope_and_token_limits_reject_before_reservation(limits):
    original = state(**limits)
    with pytest.raises(OperationalBudgetExceeded):
        reserve_request(original, request(), expected_revision=0)
    assert original.revision == 0 and original.reservations == ()


def test_uncertain_request_cannot_release_or_replay_or_free_slot():
    current = reserve_request(state(), request(), expected_revision=0)
    current = transition_request(current, "a", Status.DISPATCHED, expected_revision=1)
    current = recover_reservations(current, expected_revision=2)
    for target in (Status.RELEASED, Status.DISPATCHED, Status.SUCCEEDED):
        with pytest.raises(ValueError):
            transition_request(current, "a", target, expected_revision=current.revision)
    with pytest.raises(OperationalBudgetExceeded, match="concurrency"):
        reserve_request(current, request("b"), expected_revision=current.revision)


def test_settlement_reconciles_actual_tokens_but_preserves_request_charge():
    current = reserve_request(state(max_tokens=10), request(), expected_revision=0)
    current = transition_request(current, "a", Status.DISPATCHED, expected_revision=1)
    current = transition_request(current, "a", Status.FAILED, actual_tokens=3, expected_revision=2)
    assert current.charged_requests == 1 and current.charged_tokens == 3
    assert current.active_requests == 0
    current = reserve_request(current, request("b", reserved_tokens=7), expected_revision=3)
    assert current.charged_tokens == 10


def test_hard_token_unsupported_and_observed_overrun_fail_closed():
    with pytest.raises(OperationalBudgetExceeded, match="unsupported"):
        reserve_request(
            state(max_tokens=100), request(token_bound_guaranteed=False), expected_revision=0
        )
    current = reserve_request(state(max_tokens=100), request(), expected_revision=0)
    current = transition_request(current, "a", Status.DISPATCHED, expected_revision=1)
    current = transition_request(
        current, "a", Status.SUCCEEDED, actual_tokens=101, expected_revision=2
    )
    assert current.charged_tokens == 101 and current.token_bound_violated
    with pytest.raises(OperationalBudgetExceeded, match="declared hard token bound"):
        reserve_request(current, request("b"), expected_revision=3)


def test_attempt_ids_and_revision_cannot_be_reused_after_release():
    current = reserve_request(state(), request(), expected_revision=0)
    current = recover_reservations(current, expected_revision=1)
    with pytest.raises(ValueError, match="reused"):
        reserve_request(current, request(), expected_revision=2)
    with pytest.raises(ValueError, match="revision"):
        reserve_request(current, request("b"), expected_revision=0)
    assert current.charged_requests == current.charged_tokens == 0
