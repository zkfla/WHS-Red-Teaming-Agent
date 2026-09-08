import asyncio
from datetime import UTC, datetime

import pytest
from pydantic import BaseModel

from redteam_core.investigation.resolution import (
    ResolverDescriptor,
    ResolverOutput,
    ResolverRuntimeRegistry,
    resolve_to_observation,
)
from redteam_core.knowledge import CoreSession
from redteam_core.models.operational_budget import (
    OperationalBudgetLimits,
    ReservationStatus,
    ScopeRequestLimit,
)
from redteam_core.models.runtime import RuntimeBudgetLimits
from redteam_core.runtime import FixedClock
from redteam_core.runtime.checkpoint import (
    AtomicCheckpointStore,
    build_runtime_composition_fingerprint,
)
from redteam_core.runtime.journal import EventJournal, RuntimeEventType
from redteam_core.runtime.orchestrator import RuntimeOrchestrator
from redteam_core.runtime.resolver_dispatch import DurableResolverDispatcher
from tests.helpers import REQUIREMENT_TYPE, SamplePayload, catalog, requirement


class UnregisteredPayload(BaseModel):
    pass


class FakeResolver:
    def __init__(self, max_calls=1, max_tokens=0):
        self.descriptor = ResolverDescriptor(
            resolver_id="reference.resolver.durable@1",
            provider_id="reference.local",
            supported_requirement_type_ids=frozenset({REQUIREMENT_TYPE}),
            required_permissions=("read",),
            max_calls=max_calls,
            max_tokens_per_call=max_tokens,
        )
        self.calls = 0
        self.before = lambda: None
        self.fail = False

    async def resolve(self, requirement):
        self.before()
        self.calls += 1
        await asyncio.sleep(0)
        if self.fail:
            raise RuntimeError("remote completion unknown")
        return ResolverOutput(payload=SamplePayload(value="observed"))


def registry(resolver, *others):
    result = ResolverRuntimeRegistry()
    for item in (resolver, *others):
        result.register(item)
    result.freeze()
    return result


@pytest.fixture
def setup(tmp_path, request):
    config = getattr(request, "param", {})
    resolver = FakeResolver(
        max_calls=config.get("max_calls", 2), max_tokens=config.get("max_tokens", 0)
    )
    resolver.descriptor.timeout_seconds = config.get("timeout", 15.0)
    extras = []
    if config.get("fallback"):
        backup = FakeResolver(max_calls=config.get("max_calls", 2))
        backup.descriptor.resolver_id = "reference.resolver.backup@1"
        backup.descriptor.trust_rank = 40
        extras.append(backup)
    runtime_registry = registry(resolver, *extras)
    cat = catalog()
    clock = FixedClock(datetime(2026, 9, 8, tzinfo=UTC))
    journal = EventJournal(tmp_path / "events.jsonl", run_id="durable:one", clock=clock)
    composition = build_runtime_composition_fingerprint(
        catalog=cat, resolver_descriptors=runtime_registry.descriptors()
    )
    orchestrator = RuntimeOrchestrator(
        cat,
        composition,
        clock,
        journal,
        AtomicCheckpointStore(tmp_path / "state.json"),
        RuntimeBudgetLimits(
            max_requests=config.get("runtime_requests"), max_tokens=config.get("runtime_tokens")
        ),
    )
    runtime = orchestrator.start()
    session = CoreSession()
    req = requirement()
    session.investigation.requirements[req.requirement_id] = req
    permissions = {"read"}
    DurableResolverDispatcher.initialize(
        session=session,
        runtime=runtime,
        orchestrator=orchestrator,
        limits=OperationalBudgetLimits(
            max_requests=config.get("op_requests", 1),
            max_tokens=config.get("op_tokens", 0),
            max_concurrent=config.get("max_concurrent", 2),
            provider_limits=config.get("provider_limits", ()),
            resolver_limits=config.get("resolver_limits", ()),
        ),
    )
    dispatcher = DurableResolverDispatcher(
        session=session,
        runtime=runtime,
        orchestrator=orchestrator,
        registry=runtime_registry,
        permissions=lambda: frozenset(permissions),
    )
    yield dispatcher, resolver, req, permissions
    orchestrator.close()


async def test_adapter_enters_only_after_durable_intent_and_budget(setup):
    dispatcher, resolver, req, _ = setup

    def inspect():
        manager = dispatcher.orchestrator
        loaded = manager.checkpoint_store.load(
            catalog=manager.catalog,
            composition_fingerprint=manager.composition_fingerprint,
            journal=manager.journal,
        )
        assert (
            loaded.runtime_state.operational_budget.reservations[-1].status
            is ReservationStatus.DISPATCHED
        )
        assert manager.journal.events[-1].event_type is RuntimeEventType.OPERATIONAL_DISPATCH_INTENT

    resolver.before = inspect
    observation = await dispatcher.resolve(req.requirement_id, attempt_id="one")
    assert observation.requirement_id == req.requirement_id and resolver.calls == 1
    assert not dispatcher.session.investigation.observations  # no unauthorized semantic commit
    assert dispatcher.runtime.operational_budget.charged_requests == 1


async def test_registry_recreation_and_resume_do_not_reset_requests(setup):
    dispatcher, resolver, req, permissions = setup
    await dispatcher.resolve(req.requirement_id, attempt_id="one")
    resumed = dispatcher.orchestrator.resume(expected_run_id=dispatcher.runtime.run_id)
    recreated = registry(resolver)
    second = DurableResolverDispatcher(
        session=resumed.session,
        runtime=resumed.runtime_state,
        orchestrator=dispatcher.orchestrator,
        registry=recreated,
        permissions=lambda: frozenset(permissions),
    )
    assert recreated.call_count(resolver.descriptor.resolver_id) == 1
    with pytest.raises(RuntimeError):
        await second.resolve(req.requirement_id, attempt_id="two")
    assert resolver.calls == 1


async def test_bound_registry_cannot_use_legacy_reserve_path(setup):
    dispatcher, resolver, req, _ = setup
    with pytest.raises(RuntimeError, match="durable resolver dispatch"):
        await resolve_to_observation(
            requirement=req,
            resolver_registry=dispatcher.registry,
            catalog=dispatcher.orchestrator.catalog,
            clock=dispatcher.orchestrator.clock,
            available_permissions=frozenset({"read"}),
        )
    assert resolver.calls == 0


async def test_checkpoint_failure_prevents_call_and_poison_dispatcher(setup, monkeypatch):
    dispatcher, resolver, req, _ = setup

    def fail(*args, **kwargs):
        raise OSError("fsync unavailable")

    monkeypatch.setattr(dispatcher.orchestrator.checkpoint_store, "_atomic_write", fail)
    with pytest.raises(OSError):
        await dispatcher.resolve(req.requirement_id, attempt_id="one")
    with pytest.raises(RuntimeError, match="storage failure"):
        await dispatcher.resolve(req.requirement_id, attempt_id="two")
    assert resolver.calls == 0


async def test_permission_revocation_at_async_dispatch_boundary_prevents_call(setup, monkeypatch):
    dispatcher, resolver, req, permissions = setup
    original = dispatcher.orchestrator.journal.append
    original_wait = asyncio.wait_for

    async def scheduled_wait(awaitable, *, timeout):
        # Python's wait_for may enter immediately. Force the scheduling gap being tested.
        await asyncio.sleep(0)
        return await original_wait(awaitable, timeout=timeout)

    monkeypatch.setattr(asyncio, "wait_for", scheduled_wait)

    def append(event_type, **kwargs):
        result = original(event_type, **kwargs)
        if event_type is RuntimeEventType.OPERATIONAL_DISPATCH_INTENT:
            asyncio.get_running_loop().call_soon(permissions.clear)
        return result

    monkeypatch.setattr(dispatcher.orchestrator.journal, "append", append)
    with pytest.raises(RuntimeError):
        await dispatcher.resolve(req.requirement_id, attempt_id="one")
    assert resolver.calls == 0
    assert dispatcher.runtime.operational_budget.charged_requests == 1


async def test_simultaneous_requests_cannot_exceed_last_request(setup):
    dispatcher, resolver, req, _ = setup
    results = await asyncio.gather(
        dispatcher.resolve(req.requirement_id, attempt_id="one"),
        dispatcher.resolve(req.requirement_id, attempt_id="two"),
        return_exceptions=True,
    )
    assert sum(isinstance(result, Exception) for result in results) == 1
    assert resolver.calls == 1


async def test_unknown_result_remains_charged_across_restart(setup):
    dispatcher, resolver, req, _ = setup
    resolver.fail = True
    with pytest.raises(RuntimeError):
        await dispatcher.resolve(req.requirement_id, attempt_id="one")
    resumed = dispatcher.orchestrator.resume(expected_run_id=dispatcher.runtime.run_id)
    ledger = resumed.runtime_state.operational_budget
    assert ledger.reservations[0].status is ReservationStatus.UNCERTAIN
    assert ledger.active_requests == ledger.charged_requests == 1
    assert resolver.calls == 1


@pytest.mark.parametrize(
    "setup", [{"op_requests": 10, "max_calls": 10, "runtime_requests": 1}], indirect=True
)
async def test_global_request_limit_and_resume_usage_are_not_double_counted(setup):
    dispatcher, resolver, req, _ = setup
    await dispatcher.resolve(req.requirement_id, attempt_id="one")
    assert dispatcher.runtime.usage.requests == 1
    assert dispatcher.runtime.operational_accounted_requests == 1
    resumed = dispatcher.orchestrator.resume(expected_run_id=dispatcher.runtime.run_id)
    assert resumed.runtime_state.usage.requests == 1
    with pytest.raises(RuntimeError, match="combined runtime request"):
        await dispatcher.resolve(req.requirement_id, attempt_id="two")
    assert resolver.calls == 1


@pytest.mark.parametrize(
    "setup",
    [
        {"op_tokens": 10, "max_tokens": 6, "runtime_tokens": 5},
        {"op_tokens": None, "max_tokens": None, "runtime_tokens": 5},
    ],
    indirect=True,
)
async def test_global_token_limit_includes_unknown_or_reserved_tokens(setup):
    dispatcher, resolver, req, _ = setup
    with pytest.raises(RuntimeError, match="combined runtime"):
        await dispatcher.resolve(req.requirement_id, attempt_id="one")
    assert resolver.calls == 0 and dispatcher.runtime.operational_budget.reservations == ()


@pytest.mark.parametrize(
    "setup", [{"op_tokens": 10, "max_tokens": 6, "runtime_tokens": 8}], indirect=True
)
async def test_usage_from_other_runtime_actions_is_included(setup):
    dispatcher, resolver, req, _ = setup
    dispatcher.runtime.usage = dispatcher.runtime.usage.model_copy(update={"tokens": 3})
    with pytest.raises(RuntimeError, match="combined runtime token"):
        await dispatcher.resolve(req.requirement_id, attempt_id="one")
    assert resolver.calls == 0


@pytest.mark.parametrize(
    "setup",
    [
        {
            "op_requests": 10,
            "provider_limits": (ScopeRequestLimit(scope_id="reference.local", max_requests=0),),
        },
        {
            "op_requests": 10,
            "resolver_limits": (
                ScopeRequestLimit(scope_id="reference.resolver.durable@1", max_requests=0),
            ),
        },
    ],
    indirect=True,
)
async def test_each_scope_budget_blocks_independently_of_global_limit(setup):
    dispatcher, resolver, req, _ = setup
    with pytest.raises(RuntimeError, match="request budget"):
        await dispatcher.resolve(req.requirement_id, attempt_id="one")
    assert resolver.calls == 0


@pytest.mark.parametrize("setup", [{"op_requests": 10, "max_calls": 1}], indirect=True)
async def test_descriptor_call_limit_survives_registry_recreation_independently(setup):
    dispatcher, resolver, req, permissions = setup
    await dispatcher.resolve(req.requirement_id, attempt_id="one")
    fresh = DurableResolverDispatcher(
        session=dispatcher.session,
        runtime=dispatcher.runtime,
        orchestrator=dispatcher.orchestrator,
        registry=registry(resolver),
        permissions=lambda: frozenset(permissions),
    )
    with pytest.raises(RuntimeError, match="budget exhausted"):
        await fresh.resolve(req.requirement_id, attempt_id="two")
    assert resolver.calls == 1


@pytest.mark.parametrize("written", [False, True])
async def test_intent_journal_failure_never_calls_and_recovers_uncertain(
    setup, monkeypatch, written
):
    dispatcher, resolver, req, _ = setup
    original = dispatcher.orchestrator.journal.append

    def failing_append(event_type, **kwargs):
        if event_type is RuntimeEventType.OPERATIONAL_DISPATCH_INTENT:
            if written:
                original(event_type, **kwargs)
            raise OSError("intent fsync failed")
        return original(event_type, **kwargs)

    monkeypatch.setattr(dispatcher.orchestrator.journal, "append", failing_append)
    with pytest.raises(OSError):
        await dispatcher.resolve(req.requirement_id, attempt_id="one")
    assert resolver.calls == 0
    resumed = dispatcher.orchestrator.resume(expected_run_id=dispatcher.runtime.run_id)
    assert (
        resumed.runtime_state.operational_budget.reservations[0].status
        is ReservationStatus.UNCERTAIN
    )
    assert resumed.runtime_state.usage.requests == 1


async def test_terminal_checkpoint_failure_preserves_dispatch_charge(setup, monkeypatch):
    dispatcher, resolver, req, _ = setup
    original = dispatcher.orchestrator.checkpoint

    def failing_checkpoint(session, runtime, **kwargs):
        if runtime.operational_budget.reservations[-1].status is ReservationStatus.SUCCEEDED:
            raise OSError("terminal checkpoint failed")
        return original(session, runtime, **kwargs)

    monkeypatch.setattr(dispatcher.orchestrator, "checkpoint", failing_checkpoint)
    with pytest.raises(OSError):
        await dispatcher.resolve(req.requirement_id, attempt_id="one")
    assert resolver.calls == 1
    resumed = dispatcher.orchestrator.resume(expected_run_id=dispatcher.runtime.run_id)
    assert (
        resumed.runtime_state.operational_budget.reservations[0].status
        is ReservationStatus.UNCERTAIN
    )
    assert resumed.runtime_state.usage.requests == 1


async def test_cancellation_keeps_request_and_concurrency_reserved(setup, monkeypatch):
    dispatcher, resolver, req, _ = setup
    entered = asyncio.Event()

    async def hanging(requirement):
        resolver.calls += 1
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(resolver, "resolve", hanging)
    task = asyncio.create_task(dispatcher.resolve(req.requirement_id, attempt_id="one"))
    await asyncio.wait_for(entered.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    ledger = dispatcher.runtime.operational_budget
    assert ledger.reservations[0].status is ReservationStatus.UNCERTAIN
    assert ledger.active_requests == ledger.charged_requests == resolver.calls == 1


@pytest.mark.parametrize("setup", [{"timeout": 0.01}], indirect=True)
async def test_timeout_is_uncertain_not_refunded(setup, monkeypatch):
    dispatcher, resolver, req, _ = setup

    async def hanging(requirement):
        resolver.calls += 1
        await asyncio.Event().wait()

    monkeypatch.setattr(resolver, "resolve", hanging)
    with pytest.raises(RuntimeError, match="timed out"):
        await dispatcher.resolve(req.requirement_id, attempt_id="one")
    assert dispatcher.runtime.operational_budget.active_requests == 1
    assert resolver.calls == 1


@pytest.mark.parametrize("mutation", ["descriptor", "requirement"])
async def test_mutated_binding_after_intent_never_enters_adapter(setup, monkeypatch, mutation):
    dispatcher, resolver, req, _ = setup
    original = dispatcher.orchestrator.journal.append

    def append(event_type, **kwargs):
        result = original(event_type, **kwargs)
        if event_type is RuntimeEventType.OPERATIONAL_DISPATCH_INTENT:
            if mutation == "descriptor":
                resolver.descriptor.trust_rank += 1
            else:
                dispatcher.session.investigation.requirements.pop(req.requirement_id)
        return result

    monkeypatch.setattr(dispatcher.orchestrator.journal, "append", append)
    with pytest.raises(RuntimeError):
        await dispatcher.resolve(req.requirement_id, attempt_id="one")
    assert resolver.calls == 0
    assert dispatcher.runtime.operational_budget.charged_requests == 1


@pytest.mark.parametrize("invalid_payload", [False, True])
@pytest.mark.parametrize(
    "setup", [{"max_tokens": 10, "op_tokens": 20, "op_requests": 2}], indirect=True
)
async def test_reported_tokens_are_durable_even_when_payload_is_invalid(
    setup, monkeypatch, invalid_payload
):
    dispatcher, resolver, req, _ = setup

    async def response(requirement):
        resolver.calls += 1
        return ResolverOutput(
            payload=UnregisteredPayload() if invalid_payload else SamplePayload(value="observed"),
            actual_tokens=4,
        )

    monkeypatch.setattr(resolver, "resolve", response)
    if invalid_payload:
        with pytest.raises(RuntimeError, match="not allowed"):
            await dispatcher.resolve(req.requirement_id, attempt_id="one")
    else:
        await dispatcher.resolve(req.requirement_id, attempt_id="one")
    ledger = dispatcher.runtime.operational_budget
    expected = ReservationStatus.FAILED if invalid_payload else ReservationStatus.SUCCEEDED
    assert ledger.reservations[0].status is expected
    assert ledger.charged_tokens == dispatcher.runtime.usage.tokens == 4
    assert ledger.active_requests == 0
    resumed = dispatcher.orchestrator.resume(expected_run_id=dispatcher.runtime.run_id)
    assert resumed.runtime_state.usage.tokens == 4
    assert resumed.runtime_state.usage.requests == 1
    assert resumed.runtime_state.operational_budget.reservations[0].status is expected


@pytest.mark.parametrize(
    "setup", [{"max_tokens": 5, "op_tokens": 100, "op_requests": 2}], indirect=True
)
async def test_reported_overrun_is_preserved_and_blocks_next_dispatch(setup, monkeypatch):
    dispatcher, resolver, req, _ = setup

    async def response(requirement):
        resolver.calls += 1
        return ResolverOutput(payload=SamplePayload(value="observed"), actual_tokens=6)

    monkeypatch.setattr(resolver, "resolve", response)
    await dispatcher.resolve(req.requirement_id, attempt_id="one")
    assert dispatcher.runtime.usage.tokens == 6
    with pytest.raises(RuntimeError, match="exceeded its declared"):
        await dispatcher.resolve(req.requirement_id, attempt_id="two")
    assert resolver.calls == 1


@pytest.mark.parametrize("tokens", [-1, True, 1.2, "3"])
def test_resolver_output_rejects_invalid_usage(tokens):
    with pytest.raises(ValueError, match="nonnegative integer"):
        ResolverOutput(payload=SamplePayload(value="observed"), actual_tokens=tokens)
