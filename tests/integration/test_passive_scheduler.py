import asyncio

import pytest

from redteam_core.models.requirements import EvidenceRequirement
from redteam_core.runtime.passive_scheduler import PassiveResolutionScheduler
from redteam_core.runtime.resolution_commit import commit_resolution_receipts
from redteam_core.runtime.resolver_dispatch import DurableResolverDispatcher
from tests.integration.test_durable_resolver_dispatch import registry
from tests.integration.test_durable_resolver_dispatch import setup as durable_setup

setup = durable_setup


async def test_duplicate_waiters_share_one_call_and_one_observation(setup):
    dispatcher, resolver, req, _ = setup
    scheduler = PassiveResolutionScheduler(dispatcher, auth_context=lambda: "principal-one")
    batch = scheduler.admit(
        batch_id="one", requests=tuple((f"waiter-{n}", req.requirement_id) for n in range(20))
    )
    assert len(batch.items) == 1 and len(batch.items[0].waiter_ids) == 20
    completed = await scheduler.run("one")
    assert completed.complete and resolver.calls == 1
    result = scheduler.commit("one", expected_revision=0)
    assert len(result.recorded_observation_ids) == 1
    assert len(dispatcher.runtime.resolution_batches) == 1
    await scheduler.run("one")
    assert resolver.calls == 1


@pytest.mark.parametrize("setup", [{"op_requests": 2}], indirect=True)
@pytest.mark.parametrize("concurrency", [1, 2])
async def test_distinct_qualifiers_do_not_share_and_commit_is_atomic(setup, concurrency):
    dispatcher, resolver, req, _ = setup
    other = EvidenceRequirement.create(
        requirement_type_id=req.requirement_type_id,
        provider_id=req.provider_id,
        subject_ref=req.subject_ref,
        qualifier="other-snapshot",
        reason="separate query",
    )
    dispatcher.session.investigation.requirements[other.requirement_id] = other
    scheduler = PassiveResolutionScheduler(dispatcher, auth_context=lambda: "one")
    scheduler.admit(
        batch_id="two",
        requests=(("a", req.requirement_id), ("b", other.requirement_id)),
        concurrency=concurrency,
        per_provider_concurrency=concurrency,
    )
    await scheduler.run("two")
    assert resolver.calls == 2
    assert not dispatcher.session.investigation.observations
    result = scheduler.commit("two", expected_revision=0)
    assert len(result.recorded_observation_ids) == 2 and result.revision == 1


async def test_changed_auth_context_blocks_before_call_and_records_failure(setup):
    dispatcher, resolver, req, _ = setup
    context = ["one"]
    scheduler = PassiveResolutionScheduler(dispatcher, auth_context=lambda: context[0])
    scheduler.admit(batch_id="one", requests=(("w", req.requirement_id),))
    context[0] = "two"
    batch = await scheduler.run("one")
    assert batch.items[0].failure_kind.value == "AUTH_REQUIRED"
    assert resolver.calls == 0


async def test_permission_denial_is_durable_without_dispatch(setup):
    dispatcher, resolver, req, permissions = setup
    permissions.clear()
    scheduler = PassiveResolutionScheduler(dispatcher, auth_context=lambda: "one")
    admitted = scheduler.admit(batch_id="one", requests=(("w", req.requirement_id),))
    assert admitted.items[0].settled
    batch = await scheduler.run("one")
    assert batch.complete and batch.items[0].failure_kind.value == "AUTH_REQUIRED"
    assert resolver.calls == 0


async def test_deadline_records_uncertain_call_without_automatic_replay(setup, monkeypatch):
    dispatcher, resolver, req, _ = setup

    async def hang(requirement):
        resolver.calls += 1
        await asyncio.Event().wait()

    monkeypatch.setattr(resolver, "resolve", hang)
    scheduler = PassiveResolutionScheduler(dispatcher, auth_context=lambda: "one")
    # Leave enough admission-to-dispatch slack for checkpoint fsync on loaded CI hosts.
    scheduler.admit(batch_id="one", requests=(("w", req.requirement_id),), timeout_seconds=2)
    batch = await scheduler.run("one")
    assert batch.complete and batch.items[0].failure_kind.value == "TIMEOUT"
    assert resolver.calls == 1
    assert dispatcher.runtime.operational_budget.active_requests == 1
    await scheduler.run("one")
    assert resolver.calls == 1


async def test_admission_limits_are_checked_before_persistence(setup):
    dispatcher, resolver, req, _ = setup
    scheduler = PassiveResolutionScheduler(dispatcher, auth_context=lambda: "one")
    for requests in ((), tuple((str(n), req.requirement_id) for n in range(257))):
        with pytest.raises(ValueError):
            scheduler.admit(batch_id="one", requests=requests)
    with pytest.raises(ValueError):
        scheduler.admit(batch_id="one", requests=(("w", req.requirement_id),), concurrency=9)
    assert not dispatcher.runtime.resolution_batches and resolver.calls == 0


async def test_one_cancelled_waiter_does_not_cancel_shared_response(setup, monkeypatch):
    dispatcher, resolver, req, _ = setup
    entered, release = asyncio.Event(), asyncio.Event()
    original = resolver.resolve

    async def controlled(requirement):
        entered.set()
        await release.wait()
        return await original(requirement)

    monkeypatch.setattr(resolver, "resolve", controlled)
    scheduler = PassiveResolutionScheduler(dispatcher, auth_context=lambda: "one")
    scheduler.admit(batch_id="one", requests=(("a", req.requirement_id), ("b", req.requirement_id)))
    first = asyncio.create_task(scheduler.wait("one", "a"))
    second = asyncio.create_task(scheduler.wait("one", "b"))
    await asyncio.wait_for(entered.wait(), 5)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    release.set()
    result = await asyncio.wait_for(second, 5)
    assert result.receipt_hash is not None and resolver.calls == 1
    assert result.waiter_ids == ("a", "b")
    assert dispatcher.runtime.resolution_batches[0].cancelled_waiter_ids == ("a",)


async def test_stored_receipt_is_reconciled_after_batch_settlement_crash(setup, monkeypatch):
    dispatcher, resolver, req, permissions = setup
    scheduler = PassiveResolutionScheduler(dispatcher, auth_context=lambda: "one")
    scheduler.admit(batch_id="one", requests=(("a", req.requirement_id),))

    def crash(*args, **kwargs):
        raise OSError("batch settlement unavailable")

    monkeypatch.setattr(scheduler, "_settle", crash)
    with pytest.raises(OSError):
        await scheduler.run("one")
    assert len(dispatcher.runtime.resolution_receipts) == resolver.calls == 1
    resumed = dispatcher.orchestrator.resume(expected_run_id=dispatcher.runtime.run_id)
    dispatcher = DurableResolverDispatcher(
        session=resumed.session,
        runtime=resumed.runtime_state,
        orchestrator=dispatcher.orchestrator,
        registry=registry(resolver),
        permissions=lambda: frozenset(permissions),
    )
    scheduler = PassiveResolutionScheduler(dispatcher, auth_context=lambda: "one")
    completed = await scheduler.run("one")
    assert completed.complete and resolver.calls == 1
    scheduler.commit("one", expected_revision=0)
    assert len(dispatcher.session.investigation.observations) == 1


async def test_batch_context_cannot_be_bypassed_at_commit(setup):
    dispatcher, _, req, _ = setup
    auth = ["one"]
    scheduler = PassiveResolutionScheduler(dispatcher, auth_context=lambda: auth[0])
    batch = scheduler.admit(batch_id="one", requests=(("a", req.requirement_id),))
    await scheduler.run("one")
    with pytest.raises(PermissionError, match="current authorization"):
        commit_resolution_receipts(
            dispatcher, attempt_ids=(batch.items[0].attempt_id,), expected_revision=0
        )
    auth[0] = "two"
    with pytest.raises(PermissionError, match="context changed"):
        scheduler.commit("one", expected_revision=0)
    assert not dispatcher.session.investigation.observations


@pytest.mark.parametrize(
    "setup", [{"op_requests": 8, "max_calls": 8, "max_concurrent": 8, "timeout": 60}], indirect=True
)
@pytest.mark.parametrize("concurrency,provider_limit", [(2, 2), (4, 4), (8, 8), (4, 2)])
async def test_bounded_workers_can_run_profiles_without_exceeding_limit(
    setup, monkeypatch, concurrency, provider_limit
):
    dispatcher, resolver, req, _ = setup
    rows = []
    for n in range(concurrency):
        other = EvidenceRequirement.create(
            requirement_type_id=req.requirement_type_id,
            provider_id=req.provider_id,
            subject_ref=f"subject-{n}",
            reason="independent",
        )
        dispatcher.session.investigation.requirements[other.requirement_id] = other
        rows.append((f"waiter-{n}", other.requirement_id))
    entered, release = asyncio.Event(), asyncio.Event()
    active = peak = 0
    original = resolver.resolve

    async def controlled(requirement):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        if active == min(concurrency, provider_limit):
            entered.set()
        try:
            await release.wait()
            return await original(requirement)
        finally:
            active -= 1

    monkeypatch.setattr(resolver, "resolve", controlled)
    scheduler = PassiveResolutionScheduler(dispatcher, auth_context=lambda: "one")
    scheduler.admit(
        batch_id="profiles",
        requests=tuple(rows),
        concurrency=concurrency,
        per_provider_concurrency=provider_limit,
        timeout_seconds=120,
    )
    task = asyncio.create_task(scheduler.run("profiles"))
    try:
        await asyncio.wait_for(entered.wait(), 30)
    finally:
        release.set()
    batch = await task
    assert batch.complete and peak == min(concurrency, provider_limit)
    assert resolver.calls == concurrency
    result = scheduler.commit("profiles", expected_revision=0)
    assert len(result.recorded_observation_ids) == concurrency and result.revision == 1


async def test_batch_byte_cap_rejects_large_canonical_input_before_call(setup):
    dispatcher, resolver, req, _ = setup
    req.basis_relation_ids = ["large-ref-" * 200] * 4
    scheduler = PassiveResolutionScheduler(dispatcher, auth_context=lambda: "one")
    with pytest.raises(ValueError, match="byte budget"):
        scheduler.admit(batch_id="one", requests=(("w", req.requirement_id),), max_batch_bytes=4096)
    assert not dispatcher.runtime.resolution_batches and resolver.calls == 0


async def test_response_after_clock_deadline_is_durable_failure_not_success(setup, monkeypatch):
    from datetime import timedelta

    from redteam_core.runtime.clock import FixedClock

    dispatcher, resolver, req, _ = setup
    original = resolver.resolve
    now = dispatcher.orchestrator.clock.now()

    async def late(requirement):
        response = await original(requirement)
        dispatcher.orchestrator.clock = FixedClock(now + timedelta(seconds=61))
        return response

    monkeypatch.setattr(resolver, "resolve", late)
    scheduler = PassiveResolutionScheduler(dispatcher, auth_context=lambda: "one")
    scheduler.admit(batch_id="one", requests=(("w", req.requirement_id),), timeout_seconds=60)
    batch = await scheduler.run("one")
    receipt = dispatcher.runtime.resolution_receipts[0]
    assert receipt.observation is None and receipt.failure.kind.value == "TIMEOUT"
    assert batch.items[0].failure_kind.value == "TIMEOUT"
    scheduler.commit("one", expected_revision=0)
    assert not dispatcher.session.investigation.observations
    assert len(dispatcher.session.investigation.resolution_failures) == 1


async def test_owner_shutdown_cancels_owned_call_and_preserves_uncertainty(setup, monkeypatch):
    dispatcher, resolver, req, _ = setup
    entered = asyncio.Event()

    async def hanging(requirement):
        resolver.calls += 1
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(resolver, "resolve", hanging)
    scheduler = PassiveResolutionScheduler(dispatcher, auth_context=lambda: "one")
    scheduler.admit(batch_id="one", requests=(("w", req.requirement_id),))
    waiter = asyncio.create_task(scheduler.wait("one", "w"))
    await asyncio.wait_for(entered.wait(), 5)
    await scheduler.close()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert dispatcher.runtime.operational_budget.reservations[0].status.value == "UNCERTAIN"
    assert len(dispatcher.runtime.resolution_receipts) == resolver.calls == 1
    with pytest.raises(RuntimeError, match="closed"):
        await scheduler.run("one")
