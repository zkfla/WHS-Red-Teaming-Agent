"""Pure single-writer budget transitions. Persistence/dispatch binding is a separate boundary."""

from redteam_core.models.operational_budget import (
    OperationalBudgetState,
    RequestReservation,
    ReservationStatus,
)
from redteam_core.models.runtime import RuntimeState


class OperationalBudgetExceeded(RuntimeError):
    pass


def synchronize_operational_usage(runtime: RuntimeState) -> RuntimeState:
    """Add durable dispatch intents and measured tokens once; estimates stay in the ledger."""
    ledger = runtime.operational_budget
    if ledger is None:
        return runtime
    requests = ledger.dispatched_requests - runtime.operational_accounted_requests
    tokens = ledger.measured_tokens - runtime.operational_accounted_tokens
    if min(requests, tokens) < 0:
        raise ValueError("operational usage cannot move backwards")
    payload = runtime.model_dump(mode="python")
    payload["usage"]["requests"] += requests
    payload["usage"]["tokens"] += tokens
    payload["operational_accounted_requests"] = ledger.dispatched_requests
    payload["operational_accounted_tokens"] = ledger.measured_tokens
    return RuntimeState.model_validate(payload)


def _check_revision(state: OperationalBudgetState, expected_revision: int):
    if expected_revision != state.revision:
        raise ValueError("operational budget revision conflict")


def _replace(state: OperationalBudgetState, rows) -> OperationalBudgetState:
    payload = state.model_dump(mode="python")
    payload.update(revision=state.revision + 1, reservations=rows)
    return OperationalBudgetState.model_validate(payload)


def reserve_request(
    state: OperationalBudgetState, reservation: RequestReservation, *, expected_revision: int
) -> OperationalBudgetState:
    _check_revision(state, expected_revision)
    reservation = RequestReservation.model_validate(reservation.model_dump(mode="python"))
    if reservation.status is not ReservationStatus.RESERVED:
        raise ValueError("new request must be reserved, not pre-dispatched")
    if any(r.attempt_id == reservation.attempt_id for r in state.reservations):
        raise ValueError("attempt ID must not be reused, including released attempts")
    limits = state.limits
    if state.token_bound_violated:
        raise OperationalBudgetExceeded("provider exceeded its declared hard token bound")
    if state.charged_requests + 1 > limits.max_requests:
        raise OperationalBudgetExceeded("global request budget")
    if state.active_requests + 1 > limits.max_concurrent:
        raise OperationalBudgetExceeded("concurrency budget")
    if limits.max_tokens is not None:
        if not reservation.token_bound_guaranteed:
            raise OperationalBudgetExceeded("hard token bound unsupported")
        if state.charged_tokens + reservation.reserved_tokens > limits.max_tokens:
            raise OperationalBudgetExceeded("token budget")
    for name, scope_limits in (
        ("provider_id", limits.provider_limits),
        ("resolver_id", limits.resolver_limits),
    ):
        scope = getattr(reservation, name)
        for limit in scope_limits:
            if limit.scope_id == scope:
                used = sum(
                    getattr(r, name) == scope and r.status is not ReservationStatus.RELEASED
                    for r in state.reservations
                )
                if used + 1 > limit.max_requests:
                    raise OperationalBudgetExceeded(f"{name} request budget")
    return _replace(state, (*state.reservations, reservation))


def transition_request(
    state: OperationalBudgetState,
    attempt_id: str,
    status: ReservationStatus,
    *,
    expected_revision: int,
    actual_tokens: int | None = None,
) -> OperationalBudgetState:
    _check_revision(state, expected_revision)
    status = ReservationStatus(status)
    row = next((r for r in state.reservations if r.attempt_id == attempt_id), None)
    if row is None:
        raise ValueError("unknown operational attempt")
    allowed = {
        ReservationStatus.RESERVED: {ReservationStatus.DISPATCHED, ReservationStatus.RELEASED},
        ReservationStatus.DISPATCHED: {
            ReservationStatus.SUCCEEDED,
            ReservationStatus.FAILED,
            ReservationStatus.UNCERTAIN,
        },
    }
    if status not in allowed.get(row.status, set()):
        raise ValueError("invalid operational transition; no automatic replay or uncertain release")
    updated = RequestReservation.model_validate(
        {**row.model_dump(mode="python"), "status": status, "actual_tokens": actual_tokens}
    )
    # Preserve reported overrun; the next reservation will fail its projected budget check.
    return _replace(
        state, tuple(updated if r.attempt_id == attempt_id else r for r in state.reservations)
    )


def recover_reservations(
    state: OperationalBudgetState, *, expected_revision: int
) -> OperationalBudgetState:
    """Requires the caller's durable intent ordering; does not itself read or write storage."""
    _check_revision(state, expected_revision)
    rows = tuple(
        r.model_copy(
            update={
                "status": ReservationStatus.RELEASED
                if r.status is ReservationStatus.RESERVED
                else ReservationStatus.UNCERTAIN
                if r.status is ReservationStatus.DISPATCHED
                else r.status
            }
        )
        for r in state.reservations
    )
    return state if rows == state.reservations else _replace(state, rows)
