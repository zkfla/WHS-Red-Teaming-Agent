"""Plan 4c F14: time admission after the semaphore/cooldown wait, before reserve.

Every metered request path (structured, native, code-role, repair retries)
re-checks the engagement clock and mission state immediately before it
reserves from the shared Run ledger. Exhaustion is a structured budget stop,
never an empty success, and a denied attempt never consumes ledger budget.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from redteam.core.contracts.engagement import Engagement
from redteam.core.contracts.llm_policy import LLMRequestPolicy
from redteam.core.enums import MissionStatus
from redteam.core.ports.llm import NativeCompletion, StructuredCompletion
from redteam.orchestration.cognition import DecisionBudget, StructuredDecisionClient
from redteam.orchestration.coordination import coordinator_for
from redteam.orchestration.llm_requests import RequestAdmissionError, RunRequestLedger
from redteam.orchestration.metered_llm import RunMeteredLLM, llm_request_guard, llm_request_scope
from redteam.orchestration.reasoning_time import planning_request_guard, reasoning_time_failure
from tests.unit.test_orchestration_coordination import RUN, world


def plan4c_engagement(*, duration=None):
    budget = {"max_actions": 10}
    if duration is not None:
        budget["max_duration_seconds"] = duration
    return Engagement.model_validate({
        "engagement_id": "eng-plan4c",
        "name": "plan4c time admission",
        "scope": {"allowed_hosts": ["unit.local"]},
        "execution_budget": budget,
        "rate_limit_policy": {"max_actions_per_minute": 100},
        "approval_policy": {"auto_approve": False},
        "created_at": datetime.now(timezone.utc),
    })


def runtime_at(started_at, *, mission_id="mission-plan4c"):
    return SimpleNamespace(started_at=started_at, mission_id=mission_id)


def deps_with(engagement, *, mission_status=MissionStatus.ACTIVE):
    async def get_mission(mission_id):
        return SimpleNamespace(status=mission_status)

    return SimpleNamespace(
        engagement=engagement,
        central_state=SimpleNamespace(get_mission=get_mission),
    )


class OrderProvider:
    def __init__(self):
        self.calls = []

    async def complete(self, **kwargs):
        assert kwargs["num_retries"] == 0
        assert "admission_guard" not in kwargs
        self.calls.append("complete")
        return "ok"

    async def complete_native(self, **kwargs):
        assert kwargs["num_retries"] == 0
        assert "admission_guard" not in kwargs
        self.calls.append("complete_native")
        return NativeCompletion(tool_calls=(), text="ok")

    async def complete_structured_with_usage(self, **kwargs):
        assert kwargs["num_retries"] == 0
        assert "admission_guard" not in kwargs
        self.calls.append("structured")
        return StructuredCompletion(value={"ok": True}, total_tokens=3)


async def metered_client(tmp_path, backend, provider, *, max_requests=10):
    w = await world(tmp_path, backend)
    ledger = RunRequestLedger(coordinator_for(w.tasks, w.paths))
    await ledger.bind(RUN, LLMRequestPolicy(
        mode="bounded", max_requests=max_requests, max_concurrent_requests=1,
    ))
    return RunMeteredLLM(provider, ledger, request_semaphore=asyncio.Semaphore(1))


def test_duration_zero_with_started_at_is_exhausted():
    engagement = plan4c_engagement(duration=0)
    now = datetime.now(timezone.utc)
    runtime = runtime_at(now - timedelta(seconds=5))
    assert reasoning_time_failure(engagement, runtime, now=now) == (
        "reasoning_duration_exhausted", "budget_exhausted",
    )


def test_no_duration_or_engagement_permits():
    runtime = runtime_at(datetime.now(timezone.utc))
    assert reasoning_time_failure(plan4c_engagement(), runtime) is None
    assert reasoning_time_failure(None, runtime) is None


def test_missing_started_at_is_not_silent_success():
    engagement = plan4c_engagement(duration=60)
    failure = reasoning_time_failure(engagement, runtime_at(None))
    assert failure is not None and failure[0] == "reasoning_start_time_missing"


async def test_planning_guard_permits_and_denies():
    now = datetime.now(timezone.utc)
    runtime = runtime_at(now)
    permitted = planning_request_guard(deps_with(plan4c_engagement(duration=3600)), runtime)
    assert await permitted() is None

    exhausted = planning_request_guard(
        deps_with(plan4c_engagement(duration=0)), runtime_at(now - timedelta(seconds=1)),
    )
    with pytest.raises(RequestAdmissionError) as caught:
        await exhausted()
    assert caught.value.reason_code == "reasoning_duration_exhausted"

    paused = planning_request_guard(
        deps_with(plan4c_engagement(duration=3600), mission_status=MissionStatus.PAUSED),
        runtime,
    )
    with pytest.raises(RequestAdmissionError) as halted:
        await paused()
    assert halted.value.reason_code == "reasoning_mission_paused_or_stopped"
@pytest.mark.parametrize("backend", ["memory", "sqlite"])
async def test_guard_runs_after_cooldown_before_reserve(tmp_path, backend):
    events = []
    provider = OrderProvider()
    client = await metered_client(tmp_path, backend, provider)

    async def guard():
        events.append("guard")

    original_wait = client.ledger.wait_seconds
    original_reserve = client.ledger.reserve

    async def wait(run_id):
        events.append("wait")
        return await original_wait(run_id)

    async def reserve(run_id, **kwargs):
        events.append("reserve")
        return await original_reserve(run_id, **kwargs)

    client.ledger.wait_seconds = wait
    client.ledger.reserve = reserve
    with llm_request_scope(RUN, "plan4c"), llm_request_guard(guard):
        await client.complete(messages=[])
    assert events[:3] == ["wait", "guard", "reserve"]
    assert provider.calls == ["complete"]


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
async def test_denied_attempt_consumes_no_budget_or_request(tmp_path, backend):
    provider = OrderProvider()
    client = await metered_client(tmp_path, backend, provider, max_requests=1)

    async def deny():
        raise RequestAdmissionError("reasoning_duration_exhausted")

    with llm_request_scope(RUN, "plan4c"), llm_request_guard(deny):
        with pytest.raises(RequestAdmissionError):
            await client.complete(messages=[])
        with pytest.raises(RequestAdmissionError):
            await client.complete_native(messages=[], tools=[])
    assert provider.calls == []

    async def permit():
        return None

    with llm_request_scope(RUN, "plan4c"), llm_request_guard(permit):
        assert await client.complete(messages=[]) == "ok"
    assert provider.calls == ["complete"]


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
async def test_explicit_kwarg_guard_never_reaches_provider(tmp_path, backend):
    provider = OrderProvider()
    client = await metered_client(tmp_path, backend, provider)

    async def deny():
        raise RequestAdmissionError("reasoning_duration_exhausted")

    with llm_request_scope(RUN, "plan4c"):
        with pytest.raises(RequestAdmissionError):
            await client.complete(messages=[], admission_guard=deny)
    assert provider.calls == []
@pytest.mark.parametrize("backend", ["memory", "sqlite"])
async def test_before_call_transfers_to_post_wait_budget_stop(tmp_path, backend):
    from redteam.orchestration.cognition import DecisionFailure

    provider = OrderProvider()
    metered = await metered_client(tmp_path, backend, provider)
    now = datetime.now(timezone.utc)
    deps = deps_with(plan4c_engagement(duration=0))
    runtime = runtime_at(now - timedelta(seconds=1))
    guard = planning_request_guard(deps, runtime)
    client = StructuredDecisionClient(metered, max_attempts=2)
    with llm_request_scope(RUN, "plan4c"):
        with pytest.raises(DecisionFailure) as failed:
            await client.ask(
                stage="plan4c", payload={}, response_schema={"type": "object"},
                budget=DecisionBudget(2), before_call=guard,
            )
    assert failed.value.kind == "budget"
    assert provider.calls == []


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
async def test_client_permits_healthy_run(tmp_path, backend):
    provider = OrderProvider()
    metered = await metered_client(tmp_path, backend, provider)
    deps = deps_with(plan4c_engagement(duration=3600))
    runtime = runtime_at(datetime.now(timezone.utc))
    guard = planning_request_guard(deps, runtime)
    client = StructuredDecisionClient(metered, max_attempts=2)
    with llm_request_scope(RUN, "plan4c"):
        result = await client.ask(
            stage="plan4c", payload={}, response_schema={"type": "object"},
            budget=DecisionBudget(2), before_call=guard,
        )
    assert result == {"ok": True}
    assert provider.calls == ["structured"]
