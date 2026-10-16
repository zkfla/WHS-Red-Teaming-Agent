"""Fake dispatch only: shared caps, explicit unlimited, cancellation and restart."""

import asyncio

import pytest
from pydantic import ValidationError

from redteam.core.config import OrchestrationConfig
from redteam.core.contracts.llm_policy import LLMRequestPolicy
from redteam.core.enums import MissionStatus
from redteam.core.ports.llm import LLMProviderFailure, NativeCompletion, StructuredCompletion
from redteam.orchestration.cognition import (
    DecisionBudget,
    DecisionFailure,
    StructuredDecisionClient,
)
from redteam.orchestration.coordination import coordinator_for
from redteam.orchestration.llm_requests import RequestAdmissionError, RunRequestLedger
from redteam.orchestration.metered_llm import RunMeteredLLM, llm_request_scope
from redteam.orchestration.paths import SQLitePathStore
from redteam.orchestration.settings import OrchestrationSettings
from redteam.orchestration.task_graph.sqlite import SQLiteTaskGraphStore
from tests.unit.test_orchestration_coordination import MISSION, RUN, world


class Provider:
    def __init__(self):
        self.calls = 0
        self.entered = asyncio.Event()
        self.release = None
        self.error = None

    async def complete(self, **kwargs):
        assert kwargs["num_retries"] == 0
        self.calls += 1
        self.entered.set()
        if self.release is not None:
            await self.release.wait()
        if self.error is not None:
            raise self.error
        return "ok"

    async def complete_native(self, **kwargs):
        await self.complete(**kwargs)
        return NativeCompletion(tool_calls=(), text="ok")

    async def complete_structured_with_usage(self, **kwargs):
        await self.complete(**kwargs)
        return StructuredCompletion(value={"ok": True}, total_tokens=7)


async def test_metered_stage_attribution_and_historical_unknown_remain_separate(tmp_path):
    from redteam.orchestration.prompts import instruction_scope

    w = await world(tmp_path, "sqlite")
    ledger = RunRequestLedger(coordinator_for(w.tasks, w.paths))
    await ledger.bind(RUN, LLMRequestPolicy(mode="unlimited_requests"))
    client = RunMeteredLLM(Provider(), ledger)
    for stage in ("hypothesis_planning", "task_atomization", "task_completion_review"):
        with llm_request_scope(RUN, "same-owner"), instruction_scope(stage):
            await client.complete_structured(messages=[], response_schema={})
    # Legacy/direct calls did not record a stage: method/owner are not guessed as stage.
    with llm_request_scope(RUN, "hypothesis_planning"):
        await client.complete_native(messages=[], tools=[])
    usage = await ledger.snapshot(RUN)
    assert usage["spent"] == 4 and usage["known_tokens"] == 21 and usage["unknown_usage"] == 1
    assert usage["by_stage"]["unknown"]["spent"] == 1
    assert usage["by_stage"]["task_atomization"]["known_tokens"] == 7
    assert usage["purposes"]["planning"] == 1
    assert usage["purposes"]["atomization"] == 1
    assert usage["purposes"]["review"] == 1
    assert usage["purposes"]["other"] == 1
    assert sum(usage["purposes"].values()) + usage["unclassified_purpose"] == usage["spent"]
    assert sum(usage["outcomes"].values()) + usage["unclassified_outcome"] == usage["spent"]
    keys = ("spent", "reserved", "known_tokens", "unknown_usage", "unknown_dispatch")
    for key in keys:
        assert sum(value[key] for value in usage["by_stage"].values()) == usage[key]


async def test_metered_facade_persists_only_redacted_public_exchange_content(tmp_path):
    w = await world(tmp_path, "sqlite")
    ledger = RunRequestLedger(coordinator_for(w.tasks, w.paths))
    await ledger.bind(RUN, LLMRequestPolicy(mode="unlimited_requests"))
    client = RunMeteredLLM(Provider(), ledger)

    with llm_request_scope(RUN, "worker"):
        await client.complete_structured(
            system="Inspect the target response",
            messages=[{
                "role": "user",
                "content": "api_key=sk-request-secret-123456",
                "continuation": {"ciphertext": "provider-private-state"},
            }],
            response_schema={"type": "object"},
        )

    async def read(tx):
        rows = [
            dict(value) for (kind, _), value in tx.records.items()
            if kind == "llm_request" and value.get("run_id") == RUN
        ]
        assert len(rows) == 1
        return rows[0]

    row = await w.coordinator.change(read)
    assert "Inspect the target response" in row["request_content"]
    assert "sk-request-secret-123456" not in row["request_content"]
    assert "provider-private-state" not in row["request_content"]
    assert '\"ok\": true' in row["response_content"]
    assert row["request_truncated"] is False
    assert row["response_truncated"] is False


async def test_native_timeout_keeps_request_identity_and_classifies_transport_retry(tmp_path):
    class TimeoutThenSuccess(Provider):
        async def complete_native(self, **kwargs):
            assert kwargs["num_retries"] == 0
            self.calls += 1
            if self.calls == 1:
                await asyncio.Event().wait()
            return NativeCompletion(tool_calls=(), text="recovered", total_tokens=2)

    w = await world(tmp_path, "sqlite")
    ledger = RunRequestLedger(coordinator_for(w.tasks, w.paths))
    await ledger.bind(RUN, LLMRequestPolicy(mode="unlimited_requests"))
    provider = TimeoutThenSuccess()
    client = StructuredDecisionClient(
        RunMeteredLLM(provider, ledger), max_attempts=2, transport_limit=1,
        decision_timeout_seconds=1, llm_attempt_timeout_seconds=0.01,
    )
    with llm_request_scope(RUN, "worker"):
        result = await client.ask_native(
            messages=[], tools=[], budget=DecisionBudget(2, transport_limit=1),
        )

    assert result.text == "recovered"
    usage = await ledger.snapshot(RUN)
    assert usage["spent"] == 2 and usage["unknown_usage"] == 1
    assert usage["attempts"]["normal"] == usage["attempts"]["transport_retry"] == 1
    assert usage["outcomes"]["transport_failure"] == 1


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
async def test_interrupted_dispatch_releases_reserved_but_charges_committed_unknown(
    tmp_path, backend,
):
    w = await world(tmp_path, backend)
    ledger = RunRequestLedger(coordinator_for(w.tasks, w.paths))
    await ledger.bind(RUN, LLMRequestPolicy(mode="unlimited_requests"))

    reserved = await ledger.reserve(RUN, owner="worker", kind="complete_native")
    assert await ledger.settle_interrupted_dispatch(RUN, reserved) is False
    released = await ledger.snapshot(RUN)
    assert released["spent"] == released["reserved"] == 0

    dispatched = await ledger.reserve(RUN, owner="worker", kind="complete_native")
    await ledger.dispatch(RUN, dispatched)
    assert await ledger.settle_interrupted_dispatch(RUN, dispatched) is True
    charged = await ledger.snapshot(RUN)
    assert charged["spent"] == charged["unknown_usage"] == charged["unknown_dispatch"] == 1
    assert charged["reserved"] == 0


def test_request_record_provider_details_are_additive_and_fail_closed_when_partial():
    legacy = {
        "request_id": "request-a",
        "run_id": RUN,
        "owner": "worker",
        "kind": "complete_native",
        "state": "unknown",
        "total_tokens": None,
        "provider_outcome": "server",
    }
    assert RunRequestLedger.checked_record("request-a", dict(legacy)) == legacy

    complete = {
        **legacy,
        "provider_status": 503,
        "provider_code": "service_unavailable",
        "provider_retryable": True,
        "provider_retry_after": 2.0,
    }
    assert RunRequestLedger.checked_record("request-a", dict(complete)) == complete

    for missing in (
        "provider_status", "provider_code", "provider_retryable", "provider_retry_after",
    ):
        malformed = dict(complete)
        malformed.pop(missing)
        with pytest.raises(RequestAdmissionError, match="record_invalid"):
            RunRequestLedger.checked_record("request-a", malformed)


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
async def test_provider_failure_details_are_persisted_without_remote_material(tmp_path, backend):
    w = await world(tmp_path, backend)
    ledger = RunRequestLedger(coordinator_for(w.tasks, w.paths))
    await ledger.bind(RUN, LLMRequestPolicy(mode="unlimited_requests"))
    request = await ledger.reserve(RUN, owner="worker", kind="complete_native")
    await ledger.dispatch(RUN, request)
    await ledger.settle_provider_outcome(
        RUN,
        request,
        failure=LLMProviderFailure(
            kind="server", http_status=502, provider_code="bad_gateway",
            retryable=True, retry_after=0.5,
        ),
    )

    async def read(tx):
        return dict(tx.records[("llm_request", request)])

    row = await w.coordinator.change(read)
    assert {
        key: row[key]
        for key in (
            "provider_outcome", "provider_status", "provider_code",
            "provider_retryable", "provider_retry_after",
        )
    } == {
        "provider_outcome": "server",
        "provider_status": 502,
        "provider_code": "bad_gateway",
        "provider_retryable": True,
        "provider_retry_after": 0.5,
    }
    assert not ({"message", "body", "headers", "prompt", "response"} & set(row))


@pytest.mark.parametrize("payload", [
    {}, {"mode": "bounded"}, {"mode": "bounded", "max_requests": 0},
    {"mode": "bounded", "max_requests": -1}, {"mode": "bounded", "max_requests": True},
    {"mode": "unlimited_requests", "max_requests": 100},
    {"max_requests": 100}, {"mode": "unlimited"},
])
def test_no_implicit_unlimited_or_sentinels(payload):
    with pytest.raises(ValidationError):
        LLMRequestPolicy.model_validate(payload)


def test_config_preserves_request_policy_and_other_limits():
    config = OrchestrationConfig.model_validate({
        "max_steps": 9, "llm_request_policy": {"mode": "unlimited_requests"},
    })
    settings = OrchestrationSettings.from_config(config)
    assert settings.max_steps == 9
    assert settings.decision_max_attempts == 6
    assert settings.llm_request_policy.mode == "unlimited_requests"


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
@pytest.mark.parametrize("mode,expected", [("bounded", 100), ("unlimited_requests", 120)])
async def test_every_role_and_call_shape_share_one_atomic_pool(tmp_path, backend, mode, expected):
    w = await world(tmp_path, backend)
    ledger = RunRequestLedger(coordinator_for(w.tasks, w.paths))
    policy = LLMRequestPolicy(
        mode=mode, max_requests=100 if mode == "bounded" else None,
        max_concurrent_requests=120,
    )
    await ledger.bind(RUN, policy)
    provider = Provider()
    client = RunMeteredLLM(provider, ledger)

    async def call(index):
        with llm_request_scope(RUN, f"role-{index % 7}"):
            if index % 3 == 0:
                return await client.complete(messages=[])
            if index % 3 == 1:
                return await client.complete_native(messages=[], tools=[])
            return await client.complete_structured(messages=[], response_schema={})

    results = await asyncio.gather(*(call(i) for i in range(120)), return_exceptions=True)
    assert provider.calls == expected
    assert sum(isinstance(r, RequestAdmissionError) for r in results) == 120 - expected
    snapshot = await ledger.snapshot(RUN)
    assert snapshot["spent"] == expected
    assert snapshot["reserved"] == 0
    assert snapshot["unknown_usage"] > 0
    assert snapshot["known_tokens"] > 0


async def test_cancellation_is_charged_and_resume_does_not_reset(tmp_path):
    w = await world(tmp_path, "sqlite")
    ledger = RunRequestLedger(coordinator_for(w.tasks, w.paths))
    policy = LLMRequestPolicy(mode="bounded", max_requests=1)
    await ledger.bind(RUN, policy)
    provider = Provider()
    provider.release = asyncio.Event()
    client = RunMeteredLLM(provider, ledger)
    with llm_request_scope(RUN, "worker"):
        task = asyncio.create_task(client.complete(messages=[]))
        await provider.entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    path = tmp_path / "coordination.sqlite"
    recovered = RunRequestLedger(coordinator_for(SQLiteTaskGraphStore(path), SQLitePathStore(path)))
    await recovered.bind(RUN, policy)
    usage = await recovered.snapshot(RUN)
    assert usage["spent"] == usage["unknown_usage"] == usage["unknown_dispatch"] == 1
    assert usage["reserved"] == 0
    with pytest.raises(RequestAdmissionError, match="limit_exhausted"):
        await recovered.reserve(RUN, owner="new-worker", kind="native")
    with pytest.raises(RequestAdmissionError, match="new_run_required"):
        await recovered.bind(RUN, LLMRequestPolicy(mode="unlimited_requests"))


async def test_dispatch_failure_never_calls_provider(tmp_path):
    w = await world(tmp_path, "memory")
    ledger = RunRequestLedger(coordinator_for(w.tasks, w.paths))
    await ledger.bind(RUN, LLMRequestPolicy(mode="unlimited_requests"))
    provider = Provider()
    client = RunMeteredLLM(provider, ledger)

    async def fail(*_):
        raise OSError("test persistence failure")

    ledger.dispatch = fail
    with llm_request_scope(RUN, "planner"), pytest.raises(OSError):
        await client.complete(messages=[])
    assert provider.calls == 0
    usage = await ledger.snapshot(RUN)
    assert usage["reserved"] == usage["spent"] == usage["unknown_dispatch"] == 0


async def test_failure_after_dispatch_never_refunds_or_double_counts(tmp_path):
    w = await world(tmp_path, "memory")
    ledger = RunRequestLedger(coordinator_for(w.tasks, w.paths))
    await ledger.bind(RUN, LLMRequestPolicy(mode="unlimited_requests"))
    request = await ledger.reserve(RUN, owner="reviewer", kind="structured")
    await ledger.dispatch(RUN, request)
    with pytest.raises(RequestAdmissionError, match="refunded"):
        await ledger.release_undispatched(RUN, request)
    await ledger.settle(RUN, request, unknown=True)
    await ledger.settle(RUN, request, unknown=True)
    assert (await ledger.snapshot(RUN))["spent"] == 1
    with pytest.raises(RequestAdmissionError, match="settlement_conflict"):
        await ledger.settle(RUN, request, total_tokens=1)


async def test_unknown_crashed_dispatch_holds_slot_until_explicit_settlement(tmp_path):
    w = await world(tmp_path, "sqlite")
    ledger = RunRequestLedger(coordinator_for(w.tasks, w.paths))
    await ledger.bind(RUN, LLMRequestPolicy(mode="unlimited_requests"))
    request = await ledger.reserve(RUN, owner="planner", kind="structured")
    await ledger.dispatch(RUN, request)
    with pytest.raises(RequestAdmissionError, match="concurrency_unavailable"):
        await ledger.reserve(RUN, owner="replacement", kind="structured")
    await ledger.settle(RUN, request, unknown=True)
    next_id = await ledger.reserve(RUN, owner="replacement", kind="structured")
    await ledger.release_undispatched(RUN, next_id)
    assert (await ledger.snapshot(RUN))["spent"] == 1


@pytest.mark.parametrize("state", [MissionStatus.PAUSED, MissionStatus.CANCELLED])
async def test_unlimited_still_honors_mission_admission(tmp_path, state):
    w = await world(tmp_path, "memory")
    ledger = RunRequestLedger(coordinator_for(w.tasks, w.paths))
    await ledger.bind(RUN, LLMRequestPolicy(mode="unlimited_requests"))
    mission = await w.tasks.get_mission(MISSION)
    await w.tasks.update_mission(mission.model_copy(update={
        "status": state, "revision": mission.revision + 1,
    }))
    provider = Provider()
    client = RunMeteredLLM(provider, ledger)
    with llm_request_scope(RUN, "worker"), pytest.raises(RequestAdmissionError):
        await client.complete(messages=[])
    assert provider.calls == 0


@pytest.mark.parametrize("native", [True, False])
async def test_budget_denial_is_not_a_transport_retry(tmp_path, native):
    w = await world(tmp_path, "memory")
    ledger = RunRequestLedger(coordinator_for(w.tasks, w.paths))
    await ledger.bind(RUN, LLMRequestPolicy(mode="bounded", max_requests=1))
    provider = Provider()
    client = RunMeteredLLM(provider, ledger)
    decision = StructuredDecisionClient(client)
    with llm_request_scope(RUN, "worker"):
        await client.complete(messages=[])
        with pytest.raises(DecisionFailure) as failure:
            if native:
                await decision.ask_native(messages=[], tools=[])
            else:
                await decision.ask(stage="planner", payload={}, response_schema={})
    assert failure.value.kind == "budget"
    assert provider.calls == 1
    assert (await ledger.snapshot(RUN))["reserved"] == 0


async def test_unlimited_does_not_unbound_native_provider_retries(tmp_path):
    w = await world(tmp_path, "memory")
    ledger = RunRequestLedger(coordinator_for(w.tasks, w.paths))
    await ledger.bind(RUN, LLMRequestPolicy(mode="unlimited_requests"))
    provider = Provider()
    provider.error = LLMProviderFailure(kind="rate_limited", http_status=429, retryable=True)
    decision = StructuredDecisionClient(RunMeteredLLM(provider, ledger), max_attempts=3)
    with llm_request_scope(RUN, "worker"), pytest.raises(DecisionFailure):
        await decision.ask_native(messages=[], tools=[])
    assert provider.calls == 3
    assert (await ledger.snapshot(RUN))["spent"] == 3


async def test_shared_retry_after_threshold_recovers_through_one_request(tmp_path):
    w = await world(tmp_path, "sqlite")
    now = [100.0]
    delays = []

    async def sleep(delay):
        delays.append(delay)
        now[0] += delay

    ledger = RunRequestLedger(coordinator_for(w.tasks, w.paths), clock=lambda: now[0])
    await ledger.bind(RUN, LLMRequestPolicy(mode="unlimited_requests"))
    provider = Provider()
    provider.error = LLMProviderFailure(
        kind="rate_limited", http_status=429, retryable=True, retry_after=5,
    )
    for _ in range(3):
        client = RunMeteredLLM(provider, ledger, sleep=sleep)
        with llm_request_scope(RUN, "planner"), pytest.raises(LLMProviderFailure):
            await client.complete(messages=[])
    # Restarting the facade does not reset the shared streak, but an expired
    # retryable cooldown admits one recovery request instead of poisoning the
    # Run forever.
    provider.error = None
    with llm_request_scope(RUN, "new-worker"):
        assert await RunMeteredLLM(provider, ledger, sleep=sleep).complete(messages=[]) == "ok"
    assert delays == [5, 5, 5]
    assert provider.calls == 4
    snapshot = await ledger.snapshot(RUN)
    assert snapshot["spent"] == 4
    assert snapshot["provider_cooldown"]["state"] == "open"
    assert snapshot["provider_cooldown"]["consecutive"] == 0


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
async def test_late_success_cannot_clear_exhausted_run_wide_rate_limit(tmp_path, backend):
    w = await world(tmp_path, backend)
    now = [100.0]
    ledger = RunRequestLedger(coordinator_for(w.tasks, w.paths), clock=lambda: now[0])
    policy = LLMRequestPolicy(
        mode="unlimited_requests", max_concurrent_requests=3,
        max_consecutive_rate_limits=2,
    )
    await ledger.bind(RUN, policy)
    failure = LLMProviderFailure(kind="rate_limited", http_status=429, retryable=True,
                                 retry_after=1)
    requests = []
    for owner in ("failure-a", "failure-b", "late-success"):
        request = await ledger.reserve(RUN, owner=owner, kind="complete")
        await ledger.dispatch(RUN, request)
        requests.append(request)
    await ledger.settle_provider_outcome(RUN, requests[0], failure=failure)
    await ledger.settle_provider_outcome(RUN, requests[1], failure=failure)
    # This request was dispatched in generation 0, before generations 1/2
    # were created by the failures. Its late success cannot clear generation 2.
    await ledger.settle_provider_outcome(RUN, requests[2])
    recovered = RunRequestLedger(coordinator_for(w.tasks, w.paths))
    snapshot = await recovered.snapshot(RUN)
    assert snapshot["provider_cooldown"]["state"] == "cooldown"
    assert snapshot["provider_cooldown"]["consecutive"] == 2


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
async def test_only_one_post_threshold_recovery_request_is_reserved(tmp_path, backend):
    w = await world(tmp_path, backend)
    now = [100.0]
    ledger = RunRequestLedger(coordinator_for(w.tasks, w.paths), clock=lambda: now[0])
    policy = LLMRequestPolicy(
        mode="unlimited_requests", max_concurrent_requests=8,
        max_consecutive_rate_limits=2,
    )
    await ledger.bind(RUN, policy)
    failure = LLMProviderFailure(
        kind="rate_limited", http_status=429, retryable=True, retry_after=1,
    )
    for owner in ("failure-a", "failure-b"):
        request = await ledger.reserve(RUN, owner=owner, kind="complete")
        await ledger.dispatch(RUN, request)
        await ledger.settle_provider_outcome(RUN, request, failure=failure)
        now[0] += 1

    # A new facade/store view must observe the same persisted gate.
    ledger = RunRequestLedger(coordinator_for(w.tasks, w.paths), clock=lambda: now[0])
    results = await asyncio.gather(*(
        ledger.reserve(RUN, owner=f"recovery-{index}", kind="complete")
        for index in range(5)
    ), return_exceptions=True)
    admitted = [item for item in results if isinstance(item, str)]
    refused = [item for item in results if isinstance(item, RequestAdmissionError)]
    assert len(admitted) == 1 and len(refused) == 4
    assert {item.reason_code for item in refused} == {"llm_rate_limit_recovery_in_progress"}
    snapshot = await ledger.snapshot(RUN)
    assert snapshot["provider_cooldown"]["state"] == "half_open"
    assert snapshot["provider_cooldown"]["recovery_request_id"] == admitted[0]

    await ledger.dispatch(RUN, admitted[0])
    await ledger.settle_provider_outcome(RUN, admitted[0])
    assert (await ledger.snapshot(RUN))["provider_cooldown"]["state"] == "open"


async def test_failed_half_open_request_starts_new_bounded_cooldown(tmp_path):
    w = await world(tmp_path, "sqlite")
    now = [100.0]
    ledger = RunRequestLedger(coordinator_for(w.tasks, w.paths), clock=lambda: now[0])
    policy = LLMRequestPolicy(
        mode="unlimited_requests", max_consecutive_rate_limits=1,
        max_rate_limit_wait_seconds=10,
    )
    await ledger.bind(RUN, policy)
    failure = LLMProviderFailure(
        kind="rate_limited", http_status=429, provider_code="rate_limit",
        retryable=True, retry_after=2,
    )
    first = await ledger.reserve(RUN, owner="first", kind="complete")
    await ledger.dispatch(RUN, first)
    await ledger.settle_provider_outcome(RUN, first, failure=failure)

    with pytest.raises(RequestAdmissionError) as active:
        await ledger.reserve(RUN, owner="too-early", kind="complete")
    assert active.value.reason_code == "llm_provider_cooldown_active"
    assert active.value.diagnostic.retryable is True
    assert active.value.diagnostic.provider_status == 429
    assert active.value.diagnostic.provider_code == "rate_limit"
    assert active.value.diagnostic.retry_after == 2

    now[0] += 2
    recovery = await ledger.reserve(RUN, owner="recovery", kind="complete")
    await ledger.dispatch(RUN, recovery)
    await ledger.settle_provider_outcome(RUN, recovery, failure=failure)
    snapshot = await ledger.snapshot(RUN)
    assert snapshot["provider_cooldown"]["state"] == "cooldown"
    assert snapshot["provider_cooldown"]["generation"] == 2
    assert snapshot["provider_cooldown"]["consecutive"] == 2
    assert snapshot["provider_cooldown"]["recovery_request_id"] is None


async def test_recovery_claim_release_and_unknown_dispatch_have_distinct_resume_rules(tmp_path):
    w = await world(tmp_path, "memory")
    now = [100.0]
    ledger = RunRequestLedger(coordinator_for(w.tasks, w.paths), clock=lambda: now[0])
    await ledger.bind(RUN, LLMRequestPolicy(
        mode="unlimited_requests", max_consecutive_rate_limits=1,
    ))
    failure = LLMProviderFailure(
        kind="rate_limited", http_status=429, retryable=True, retry_after=1,
    )
    first = await ledger.reserve(RUN, owner="first", kind="complete")
    await ledger.dispatch(RUN, first)
    await ledger.settle_provider_outcome(RUN, first, failure=failure)

    async def launch_reason(tx):
        return ledger.launch_blocked(tx, RUN)

    assert await w.coordinator.change(launch_reason) == "llm_provider_cooldown_active"
    now[0] += 1
    assert await w.coordinator.change(launch_reason) is None

    never_dispatched = await ledger.reserve(RUN, owner="reserved", kind="complete")
    assert await w.coordinator.change(launch_reason) == "llm_rate_limit_recovery_in_progress"
    await ledger.release_undispatched(RUN, never_dispatched)
    released = await ledger.snapshot(RUN)
    assert released["provider_cooldown"]["state"] == "cooldown"
    assert released["provider_cooldown"]["recovery_request_id"] is None

    possibly_dispatched = await ledger.reserve(RUN, owner="uncertain", kind="complete")
    await ledger.dispatch(RUN, possibly_dispatched)
    await ledger.recover_owner(RUN, owner="uncertain")
    held = await ledger.snapshot(RUN)
    assert held["provider_cooldown"]["state"] == "held"
    assert held["unknown_dispatch"] == 2  # original 429 plus uncertain recovery
    with pytest.raises(RequestAdmissionError, match="operator_review"):
        await ledger.wait_seconds(RUN)


def test_legacy_cooldown_row_is_read_as_recoverable_v2_state():
    state = RunRequestLedger.checked_cooldown({
        "consecutive": 3, "until": 100.0, "held": False,
    })
    assert state["version"] == 2 and state["state"] == "cooldown"
    assert state["generation"] == 0 and state["consecutive"] == 3
    assert state["last_failure"] == {
        "kind": "rate_limited", "provider_status": 429, "provider_code": "unknown",
        "retryable": True, "retry_after": None,
    }


@pytest.mark.parametrize("retryable,delay", [(False, 1), (True, 120)])
async def test_permanent_or_long_retry_after_holds_not_tight_retries(tmp_path, retryable, delay):
    w = await world(tmp_path, "memory")
    ledger = RunRequestLedger(coordinator_for(w.tasks, w.paths))
    await ledger.bind(RUN, LLMRequestPolicy(mode="unlimited_requests"))
    provider = Provider()
    provider.error = LLMProviderFailure(
        kind="rate_limited", http_status=429, retryable=retryable, retry_after=delay,
    )
    client = RunMeteredLLM(provider, ledger)
    with llm_request_scope(RUN, "planner"):
        with pytest.raises(LLMProviderFailure):
            await client.complete(messages=[])
        with pytest.raises(RequestAdmissionError, match="operator_review"):
            await client.complete(messages=[])
    assert provider.calls == 1


async def test_unscoped_calls_refused_and_context_restored(tmp_path):
    w = await world(tmp_path, "memory")
    ledger = RunRequestLedger(coordinator_for(w.tasks, w.paths))
    await ledger.bind(RUN, LLMRequestPolicy(mode="unlimited_requests"))
    provider = Provider()
    client = RunMeteredLLM(provider, ledger)
    with pytest.raises(RequestAdmissionError, match="scope_missing"):
        await client.complete(messages=[])
    with llm_request_scope(RUN, "planner"):
        await client.complete(messages=[])
    with pytest.raises(RequestAdmissionError, match="scope_missing"):
        await client.complete(messages=[])
    assert provider.calls == 1
