from datetime import UTC, datetime

import pytest

from redteam_core.knowledge import CoreSession
from redteam_core.models.operational_budget import (
    OperationalBudgetLimits,
    OperationalBudgetState,
    RequestReservation,
)
from redteam_core.models.operational_budget import (
    ReservationStatus as Status,
)
from redteam_core.models.runtime import RuntimeState
from redteam_core.runtime import FixedClock
from redteam_core.runtime.checkpoint import (
    AtomicCheckpointStore,
    CheckpointEnvelope,
    build_runtime_composition_fingerprint,
    resume_checkpoint,
)
from redteam_core.runtime.journal import EventJournal, RuntimeEvent, RuntimeEventType
from redteam_core.runtime.operational_budget import reserve_request, transition_request
from tests.helpers import catalog


def checkpoint(tmp_path, status=Status.RESERVED):
    cat = catalog()
    clock = FixedClock(datetime(2026, 9, 8, tzinfo=UTC))
    fingerprint = build_runtime_composition_fingerprint(catalog=cat)
    journal = EventJournal(tmp_path / "journal.jsonl", run_id="budget:run", clock=clock)
    budget = OperationalBudgetState(
        run_id=journal.run_id,
        composition_hash=fingerprint.composition_hash,
        limits=OperationalBudgetLimits(max_requests=1, max_tokens=10),
    )
    budget = reserve_request(
        budget,
        RequestReservation(
            attempt_id="attempt:one",
            provider_id="provider:one",
            resolver_id="resolver:one",
            reserved_tokens=10,
            token_bound_guaranteed=True,
        ),
        expected_revision=0,
    )
    if status is not Status.RESERVED:
        budget = transition_request(budget, "attempt:one", Status.DISPATCHED, expected_revision=1)
    if status is Status.SUCCEEDED:
        budget = transition_request(
            budget, "attempt:one", status, actual_tokens=4, expected_revision=2
        )
    runtime = RuntimeState(run_id=journal.run_id, operational_budget=budget)
    store = AtomicCheckpointStore(tmp_path / "checkpoint.json")
    envelope = store.write(
        session=CoreSession(),
        runtime_state=runtime,
        catalog=cat,
        composition_fingerprint=fingerprint,
        journal=journal,
        clock=clock,
        reason="operational-budget-test",
    )
    kwargs = dict(
        catalog=cat,
        composition_fingerprint=fingerprint,
        journal=journal,
        clock=clock,
        expected_run_id=journal.run_id,
    )
    return store, envelope, kwargs


@pytest.mark.parametrize(
    "before,after,requests,tokens",
    [
        (Status.RESERVED, Status.RELEASED, 0, 0),
        (Status.DISPATCHED, Status.UNCERTAIN, 1, 10),
        (Status.SUCCEEDED, Status.SUCCEEDED, 1, 4),
    ],
)
def test_actual_checkpoint_resume_preserves_budget_or_releases_only_undispatched(
    tmp_path,
    before,
    after,
    requests,
    tokens,
):
    store, envelope, kwargs = checkpoint(tmp_path, before)
    restored = resume_checkpoint(store, **kwargs).runtime_state.operational_budget
    assert restored.reservations[0].status is after
    assert restored.charged_requests == requests
    assert restored.charged_tokens == tokens
    assert envelope.runtime_state.operational_budget.reservations[0].status is before


def test_post_checkpoint_intent_prevents_false_refund(tmp_path):
    store, envelope, kwargs = checkpoint(tmp_path)
    kwargs["journal"].append(
        RuntimeEventType.OPERATIONAL_DISPATCH_INTENT,
        attributes={
            "attempt_id": "attempt:one",
            "provider_id": "provider:one",
            "resolver_id": "resolver:one",
            "composition_hash": kwargs["composition_fingerprint"].composition_hash,
            "limits_hash": envelope.runtime_state.operational_budget.limits_hash,
        },
    )
    restored = resume_checkpoint(store, **kwargs).runtime_state.operational_budget
    assert restored.charged_requests == 1 and restored.charged_tokens == 10
    assert restored.reservations[0].status is Status.UNCERTAIN


@pytest.mark.parametrize(
    "field,value",
    [
        ("attempt_id", "unknown"),
        ("provider_id", "different"),
        ("limits_hash", "sha256:" + "f" * 64),
        ("composition_hash", "sha256:" + "0" * 64),
    ],
)
def test_unknown_or_rebound_intent_fails_closed(tmp_path, field, value):
    store, envelope, kwargs = checkpoint(tmp_path)
    attributes = {
        "attempt_id": "attempt:one",
        "provider_id": "provider:one",
        "resolver_id": "resolver:one",
        "composition_hash": kwargs["composition_fingerprint"].composition_hash,
        "limits_hash": envelope.runtime_state.operational_budget.limits_hash,
    }
    attributes[field] = value
    kwargs["journal"].append(RuntimeEventType.OPERATIONAL_DISPATCH_INTENT, attributes=attributes)
    with pytest.raises(ValueError):
        resume_checkpoint(store, **kwargs)


@pytest.mark.parametrize("old_checkpoint_schema", ["1.0", "2.0", "2.1", "2.2"])
def test_old_checkpoint_and_event_schema_are_not_migrated(tmp_path, old_checkpoint_schema):
    _, envelope, kwargs = checkpoint(tmp_path)
    data = envelope.model_dump(mode="python")
    data["schema_version"] = old_checkpoint_schema
    with pytest.raises(ValueError, match="unsupported checkpoint schema"):
        CheckpointEnvelope.model_validate(data)
    event = kwargs["journal"].events[-1].model_dump(mode="python")
    event["schema_version"] = "1.0"
    with pytest.raises(ValueError, match="unsupported.*schema"):
        RuntimeEvent.model_validate(event)


def test_other_run_budget_is_rejected_by_runtime_state(tmp_path):
    _, envelope, _ = checkpoint(tmp_path)
    with pytest.raises(ValueError, match="run binding"):
        RuntimeState(
            run_id="different", operational_budget=envelope.runtime_state.operational_budget
        )


def test_settlement_marker_without_dispatch_cannot_refund_reserved_budget(tmp_path):
    store, _, kwargs = checkpoint(tmp_path)
    kwargs["journal"].append(
        RuntimeEventType.OPERATIONAL_REQUEST_SETTLED,
        attributes={
            "attempt_id": "attempt:one",
            "provider_id": "provider:one",
            "resolver_id": "resolver:one",
            "composition_hash": kwargs["composition_fingerprint"].composition_hash,
        },
    )
    with pytest.raises(ValueError, match="without a known operational dispatch"):
        resume_checkpoint(store, **kwargs)
