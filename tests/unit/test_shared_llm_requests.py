"""Fake dispatch only: shared caps, explicit unlimited, cancellation and restart."""

import asyncio

import pytest
from pydantic import ValidationError

from redteam.core.config import OrchestrationConfig
from redteam.core.contracts.llm_policy import LLMRequestPolicy
from redteam.core.enums import MissionStatus
from redteam.core.ports.llm import LLMProviderFailure, NativeCompletion, StructuredCompletion
from redteam.orchestration.cognition import DecisionFailure, StructuredDecisionClient
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
    keys = ("spent", "reserved", "known_tokens", "unknown_usage", "unknown_dispatch")
    for key in keys:
        assert sum(value[key] for value in usage["by_stage"].values()) == usage[key]


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
    assert (await ledger.snapshot(RUN))["reserved"] == 1


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


async def test_shared_retry_after_wait_and_consecutive_limit_survive_new_client(tmp_path):
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
    # Restarting the facade does not reset the shared 429 counter.
    with llm_request_scope(RUN, "new-worker"), pytest.raises(
        RequestAdmissionError, match="consecutive_rate_limit",
    ):
        await RunMeteredLLM(provider, ledger, sleep=sleep).complete(messages=[])
    assert delays == [5, 5]
    assert provider.calls == 3
    assert (await ledger.snapshot(RUN))["spent"] == 3


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
async def test_late_success_cannot_clear_exhausted_run_wide_rate_limit(tmp_path, backend):
    w = await world(tmp_path, backend)
    ledger = RunRequestLedger(coordinator_for(w.tasks, w.paths))
    policy = LLMRequestPolicy(mode="unlimited_requests", max_consecutive_rate_limits=2)
    await ledger.bind(RUN, policy)
    failure = LLMProviderFailure(kind="rate_limited", http_status=429, retryable=True,
                                 retry_after=1)
    await ledger.record_provider_outcome(RUN, failure)
    await ledger.record_provider_outcome(RUN, failure)
    # A success from a request dispatched before the hold is NOT a new permit.
    await ledger.record_provider_outcome(RUN)
    recovered = RunRequestLedger(coordinator_for(w.tasks, w.paths))
    with pytest.raises(RequestAdmissionError, match="consecutive_rate_limit"):
        await recovered.wait_seconds(RUN)


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
