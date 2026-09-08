import pytest

from redteam_core.investigation.resolution import ResolverOutput
from redteam_core.runtime.passive_scheduler import PassiveResolutionScheduler
from tests.integration.test_durable_resolver_dispatch import UnregisteredPayload
from tests.integration.test_durable_resolver_dispatch import setup as durable_setup

setup = durable_setup


@pytest.mark.parametrize("setup", [{"fallback": True, "op_requests": 2}], indirect=True)
@pytest.mark.parametrize(
    "enabled,outcome", [(True, "invalid"), (False, "invalid"), (True, "uncertain")]
)
async def test_fallback_is_explicit_bounded_and_preserves_first_failure(
    setup, monkeypatch, enabled, outcome
):
    dispatcher, primary, req, _ = setup
    backup = dispatcher.registry.eligible(req, available_permissions=frozenset({"read"}))[1]

    async def first(requirement):
        primary.calls += 1
        if outcome == "uncertain":
            raise RuntimeError("remote result unknown")
        return ResolverOutput(payload=UnregisteredPayload())

    monkeypatch.setattr(primary, "resolve", first)
    scheduler = PassiveResolutionScheduler(dispatcher, auth_context=lambda: "one")
    admitted = scheduler.admit(
        batch_id="one", requests=(("w", req.requirement_id),), allow_fallback=enabled
    )
    batch = await scheduler.run("one")
    expected = 2 if enabled and outcome == "invalid" else 1
    assert primary.calls == 1 and backup.calls == expected - 1
    assert dispatcher.runtime.usage.requests == expected
    assert len(batch.items[0].receipt_hashes) == expected
    assert len(dispatcher.runtime.operational_budget.reservations) == expected
    scheduler.commit("one", expected_revision=0)
    assert len(dispatcher.session.investigation.resolution_failures) == 1
    assert len(dispatcher.session.investigation.observations) == expected - 1
    if expected == 2:
        assert len(admitted.items[0].attempt_ids) == 2
        assert dispatcher.runtime.operational_budget.reservations[0].status.value == "FAILED"
        assert dispatcher.runtime.operational_budget.reservations[1].status.value == "SUCCEEDED"
    if outcome == "uncertain":
        assert dispatcher.runtime.operational_budget.active_requests == 1


@pytest.mark.parametrize("setup", [{"fallback": True, "op_requests": 1}], indirect=True)
async def test_fallback_cannot_escape_global_request_budget(setup, monkeypatch):
    dispatcher, primary, req, _ = setup
    backup = dispatcher.registry.eligible(req, available_permissions=frozenset({"read"}))[1]

    async def first(requirement):
        primary.calls += 1
        return ResolverOutput(payload=UnregisteredPayload())

    monkeypatch.setattr(primary, "resolve", first)
    scheduler = PassiveResolutionScheduler(dispatcher, auth_context=lambda: "one")
    scheduler.admit(batch_id="one", requests=(("w", req.requirement_id),), allow_fallback=True)
    batch = await scheduler.run("one")
    assert batch.items[0].failure_kind.value == "BUDGET_EXHAUSTED"
    assert primary.calls == 1 and backup.calls == 0
    assert dispatcher.runtime.usage.requests == 1
    scheduler.commit("one", expected_revision=0)
    assert len(dispatcher.session.investigation.resolution_failures) == 1


@pytest.mark.parametrize("setup", [{"fallback": True, "op_requests": 2}], indirect=True)
async def test_all_known_fallback_failures_are_committed_without_observation(setup, monkeypatch):
    dispatcher, primary, req, _ = setup
    backup = dispatcher.registry.eligible(req, available_permissions=frozenset({"read"}))[1]

    async def invalid(resolver, requirement):
        resolver.calls += 1
        return ResolverOutput(payload=UnregisteredPayload())

    monkeypatch.setattr(primary, "resolve", lambda requirement: invalid(primary, requirement))
    monkeypatch.setattr(backup, "resolve", lambda requirement: invalid(backup, requirement))
    scheduler = PassiveResolutionScheduler(dispatcher, auth_context=lambda: "one")
    scheduler.admit(batch_id="one", requests=(("w", req.requirement_id),), allow_fallback=True)
    batch = await scheduler.run("one")
    assert len(batch.items[0].receipt_hashes) == primary.calls + backup.calls == 2
    scheduler.commit("one", expected_revision=0)
    assert len(dispatcher.session.investigation.resolution_failures) == 2
    assert not dispatcher.session.investigation.observations


@pytest.mark.parametrize("setup", [{"fallback": True, "op_requests": 2}], indirect=True)
async def test_crash_between_known_failure_and_fallback_resumes_without_recalling_first(
    setup, monkeypatch
):
    from redteam_core.runtime.resolver_dispatch import DurableResolverDispatcher
    from tests.integration.test_durable_resolver_dispatch import registry

    dispatcher, primary, req, permissions = setup
    backup = dispatcher.registry.eligible(req, available_permissions=frozenset({"read"}))[1]

    async def invalid(requirement):
        primary.calls += 1
        return ResolverOutput(payload=UnregisteredPayload())

    monkeypatch.setattr(primary, "resolve", invalid)
    scheduler = PassiveResolutionScheduler(dispatcher, auth_context=lambda: "one")
    scheduler.admit(batch_id="one", requests=(("w", req.requirement_id),), allow_fallback=True)
    original = dispatcher.resolve
    calls = 0

    async def crash_before_second(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("process stopped before fallback dispatch")
        return await original(*args, **kwargs)

    monkeypatch.setattr(dispatcher, "resolve", crash_before_second)
    with pytest.raises(OSError):
        await scheduler.run("one")
    assert primary.calls == 1 and backup.calls == 0
    resumed = dispatcher.orchestrator.resume(expected_run_id=dispatcher.runtime.run_id)
    fresh_registry = registry(primary, backup)
    recovered_dispatcher = DurableResolverDispatcher(
        session=resumed.session,
        runtime=resumed.runtime_state,
        orchestrator=dispatcher.orchestrator,
        registry=fresh_registry,
        permissions=lambda: frozenset(permissions),
    )
    recovered = PassiveResolutionScheduler(recovered_dispatcher, auth_context=lambda: "one")
    batch = await recovered.run("one")
    assert batch.complete and primary.calls == backup.calls == 1
    recovered.commit("one", expected_revision=0)
    assert len(recovered_dispatcher.session.investigation.resolution_failures) == 1
    assert len(recovered_dispatcher.session.investigation.observations) == 1
