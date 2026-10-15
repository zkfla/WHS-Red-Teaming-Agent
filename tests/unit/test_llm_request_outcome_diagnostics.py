"""Unknown-settlement cause categories: sanitized, persisted, and never inferred."""

import asyncio
import json

import pytest

from redteam.core.contracts.llm_policy import LLMRequestPolicy
from redteam.core.ports.llm import LLMProviderFailure, NativeCompletion
from redteam.orchestration.cognition import StructuredDecisionClient
from redteam.orchestration.coordination import coordinator_for
from redteam.orchestration.llm_requests import RequestAdmissionError, RunRequestLedger
from redteam.orchestration.metered_llm import RunMeteredLLM, llm_request_scope
from redteam.orchestration.paths import SQLitePathStore
from redteam.orchestration.task_graph.sqlite import SQLiteTaskGraphStore
from tests.unit.test_orchestration_coordination import RUN, world

RAW = "RAW_PROMPT_BODY_SECRET_MARKER"
# 2026-10-15T00:00:00Z as Unix UTC seconds, the unit the ledger clock returns.
UNIX_BASE = 1792022400.0


class Scripted:
    def __init__(self, script):
        self.script = list(script)

    async def complete_native(self, **kwargs):
        assert kwargs["num_retries"] == 0
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


class Hanging:
    def __init__(self):
        self.entered = asyncio.Event()

    async def complete_native(self, **kwargs):
        self.entered.set()
        await asyncio.Event().wait()


async def stored_rows(ledger):
    async def read(tx):
        return RunRequestLedger._rows(tx, RUN)
    return await ledger.coordinator.change(read)


async def read_one(ledger, request_id):
    rows = await stored_rows(ledger)
    return next(row for row in rows if row["request_id"] == request_id)


async def new_ledger(tmp_path, *, clock=lambda: UNIX_BASE):
    w = await world(tmp_path, "sqlite")
    ledger = RunRequestLedger(coordinator_for(w.tasks, w.paths), clock=clock)
    await ledger.bind(RUN, LLMRequestPolicy(mode="unlimited_requests"))
    return ledger


async def test_retried_provider_failure_keeps_observed_cause_across_restart(tmp_path):
    ledger = await new_ledger(tmp_path)
    failure = LLMProviderFailure(
        kind="rate_limited", http_status=429, provider_code="rate_limit_exceeded",
        retryable=True, retry_after=0.0,
    )
    client = RunMeteredLLM(Scripted([failure, NativeCompletion(tool_calls=(), text="ok")]), ledger)
    with llm_request_scope(RUN, "worker"):
        with pytest.raises(LLMProviderFailure):
            await client.complete_native(messages=[], tools=[])
        await client.complete_native(messages=[], tools=[])
    path = tmp_path / "coordination.sqlite"
    recovered = RunRequestLedger(
        coordinator_for(SQLiteTaskGraphStore(path), SQLitePathStore(path)),
        clock=lambda: UNIX_BASE,
    )
    rows = {row["state"]: row for row in await stored_rows(recovered)}
    assert rows["settled"]["total_tokens"] is None and "outcome" not in rows["settled"]
    failed = rows["unknown"]
    assert failed["outcome"] == {
        "category": "provider_failure", "source": "facade", "kind": "rate_limited",
        "http_status": 429, "provider_code": "rate_limit_exceeded",
        "retryable": True, "retry_after": 0.0,
    }
    assert failed["dispatched_at"] == failed["settled_at"] == UNIX_BASE
    usage = await recovered.snapshot(RUN)
    # The native retry carries no token count, so both rows are charged with unknown usage.
    assert usage["spent"] == 2 and usage["unknown_usage"] == 2
    assert usage["unknown_outcomes"] == {"provider_failure": 1}


async def test_decision_client_retry_keeps_provider_cause_after_budget_is_discarded(tmp_path):
    ledger = await new_ledger(tmp_path)
    failure = LLMProviderFailure(
        kind="rate_limited", http_status=429, provider_code="rate_limit_exceeded",
        retryable=True, retry_after=0.0,
    )
    provider = Scripted([failure, NativeCompletion(tool_calls=(), text="ok")])
    decision = StructuredDecisionClient(RunMeteredLLM(provider, ledger), max_attempts=3)
    with llm_request_scope(RUN, "worker"):
        response = await decision.ask_native(messages=[], tools=[])
    assert response.text == "ok"
    rows = {row["state"]: row for row in await stored_rows(ledger)}
    assert rows["unknown"]["outcome"] == {
        "category": "provider_failure", "source": "facade", "kind": "rate_limited",
        "http_status": 429, "provider_code": "rate_limit_exceeded",
        "retryable": True, "retry_after": 0.0,
    }
    usage = await ledger.snapshot(RUN)
    assert usage["spent"] == 2 and usage["unknown_outcomes"] == {"provider_failure": 1}


@pytest.mark.parametrize(("exc", "category"), [
    (asyncio.TimeoutError(RAW), "timeout"),
    (TimeoutError(RAW), "timeout"),
    (RuntimeError(RAW), "exception"),
])
async def test_timeout_and_other_exceptions_store_category_without_raw_text(
    tmp_path, exc, category,
):
    ledger = await new_ledger(tmp_path)
    client = RunMeteredLLM(Scripted([exc]), ledger)
    with llm_request_scope(RUN, "worker"), pytest.raises(type(exc)):
        await client.complete_native(messages=[], tools=[])
    (row,) = await stored_rows(ledger)
    assert row["outcome"] == {"category": category, "source": "facade"}
    dumped = json.dumps(row, default=str)
    assert RAW not in dumped and type(exc).__name__ not in dumped


async def test_known_cancellation_is_classified_and_still_charged(tmp_path):
    ledger = await new_ledger(tmp_path)
    provider = Hanging()
    client = RunMeteredLLM(provider, ledger)
    with llm_request_scope(RUN, "worker"):
        task = asyncio.create_task(client.complete_native(messages=[], tools=[]))
        await provider.entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    (row,) = await stored_rows(ledger)
    assert row["state"] == "unknown"
    assert row["outcome"] == {"category": "cancelled", "source": "facade"}


async def test_recovered_abandoned_dispatch_is_outcome_lost_not_a_provider_cause(tmp_path):
    ledger = await new_ledger(tmp_path, clock=lambda: UNIX_BASE + 50.0)
    request = await ledger.reserve(RUN, owner="planner", kind="structured")
    await ledger.dispatch(RUN, request)
    await ledger.recover_run(RUN)
    row = await read_one(ledger, request)
    assert row["state"] == "unknown" and row["total_tokens"] is None
    assert row["outcome"] == {"category": "outcome_lost", "source": "recovery"}
    assert row["dispatched_at"] == UNIX_BASE + 50.0 and "settled_at" not in row
    assert (await ledger.snapshot(RUN))["unknown_outcomes"] == {"outcome_lost": 1}


async def test_malformed_outcome_and_timestamps_are_rejected(tmp_path):
    ledger = await new_ledger(tmp_path)
    request = await ledger.reserve(RUN, owner="worker", kind="native")
    await ledger.dispatch(RUN, request)
    await ledger.settle(
        RUN, request, unknown=True, outcome={"category": "timeout", "source": "facade"},
    )
    original = await read_one(ledger, request)

    async def write(tx, patch):
        tx.records[("llm_request", request)] = {**original, **patch}

    def setter(patch):
        async def apply(tx):
            await write(tx, patch)
        return apply

    malformed = [
        {"outcome": {"category": "timeout", "source": "facade", "message": RAW}},
        {"outcome": {"category": "outcome_lost", "source": "facade"}},
        {"outcome": {"category": "provider_failure", "source": "facade", "kind": "rate_limited",
                     "http_status": "429", "provider_code": "x", "retryable": True,
                     "retry_after": None}},
        {"dispatched_at": True},
        {"settled_at": -1.0},
    ]
    for patch in malformed:
        await ledger.coordinator.change(setter(patch))
        with pytest.raises(RequestAdmissionError, match="record_invalid"):
            await stored_rows(ledger)
    await ledger.coordinator.change(setter({}))
    assert (await read_one(ledger, request))["outcome"]["category"] == "timeout"
    with pytest.raises(RequestAdmissionError, match="outcome_invalid"):
        other = await ledger.reserve(RUN, owner="worker", kind="native")
        await ledger.dispatch(RUN, other)
        await ledger.settle(RUN, other, outcome={"category": "timeout", "source": "facade"})


async def test_legacy_unknown_rows_stay_readable_without_inferred_cause(tmp_path):
    ledger = await new_ledger(tmp_path)
    legacy = {
        "request_id": "llm_request_legacy", "run_id": RUN, "owner": "planner",
        "kind": "structured", "state": "unknown", "total_tokens": None,
        "stage": "hypothesis_planning",
    }

    async def insert(tx):
        tx.records[("llm_request", legacy["request_id"])] = dict(legacy)

    await ledger.coordinator.change(insert)
    usage = await ledger.snapshot(RUN)
    assert usage["spent"] == 1 and usage["unknown_usage"] == 1
    assert usage["unknown_outcomes"] == {"unrecorded": 1}
