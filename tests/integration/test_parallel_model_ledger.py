"""Real SQLite admission under parallel model calls; provider I/O is synthetic."""

import asyncio

import pytest

from redteam.core.contracts.llm_policy import LLMRequestPolicy
from redteam.core.ports.llm import StructuredCompletion
from redteam.core.request_scope import current_request_scope, llm_request_scope
from redteam.orchestration.coordination import coordinator_for
from redteam.orchestration.llm_requests import RequestAdmissionError, RunRequestLedger
from redteam.orchestration.metered_llm import RunMeteredLLM
from redteam.orchestration.paths import SQLitePathStore
from redteam.orchestration.prompts import instruction_scope, instruction_selection
from redteam.orchestration.task_graph.sqlite import SQLiteTaskGraphStore
from tests.unit.test_orchestration_coordination import RUN, world


class BarrierProvider:
    """One-call fake provider that exposes overlap and request-local context."""

    def __init__(self):
        self.gates = {
            "overlap": asyncio.Event(),
            "budget": asyncio.Event(),
        }
        self.entered = asyncio.Queue()
        self.active = 0
        self.peak_active = 0
        self.contexts = {}

    async def complete_structured_with_usage(
        self, *, messages, response_schema, system=None, num_retries,
    ):
        assert num_retries == 0
        message = messages[0]["content"]
        owner = message["owner"]
        phase = message["phase"]
        scope = current_request_scope()
        stage, role_id = instruction_selection()
        self.contexts[owner] = {
            "run_id": scope.run_id,
            "owner": scope.owner,
            "stage": stage,
            "role_id": role_id,
            "phase": phase,
        }
        self.active += 1
        self.peak_active = max(self.peak_active, self.active)
        self.entered.put_nowait((phase, owner))
        try:
            await self.gates[phase].wait()
        finally:
            self.active -= 1
        return StructuredCompletion(
            value={"owner": owner, "phase": phase},
            total_tokens=message["tokens"],
        )


async def _wait_for_phase(provider, phase, count):
    seen = []
    while len(seen) < count:
        entered_phase, owner = await asyncio.wait_for(provider.entered.get(), 5)
        if entered_phase == phase:
            seen.append(owner)
    return seen


async def _request(client, owner, stage, phase, tokens):
    with llm_request_scope(RUN, owner), instruction_scope(stage):
        return await client.complete_structured_with_usage(
            messages=[{
                "role": "user",
                "content": {"owner": owner, "phase": phase, "tokens": tokens},
            }],
            response_schema={"type": "object"},
        )


async def _request_rows(coordinator):
    async def read(tx):
        return [
            dict(row)
            for (kind, _), row in tx.records.items()
            if kind == "llm_request" and row.get("run_id") == RUN
        ]

    return await coordinator.change(read)


@pytest.mark.parametrize("concurrency", [3, 4, 6])
async def test_parallel_model_calls_share_sqlite_admission_and_budget(tmp_path, concurrency):
    state = await world(tmp_path, "sqlite")
    policy = LLMRequestPolicy(
        mode="bounded",
        max_requests=concurrency * 2,
        max_concurrent_requests=concurrency,
    )
    ledger = RunRequestLedger(coordinator_for(state.tasks, state.paths))
    await ledger.bind(RUN, policy)
    provider = BarrierProvider()
    client = RunMeteredLLM(provider, ledger)

    # Hold exactly the configured number in the provider to prove real overlap.
    first_wave = [
        asyncio.create_task(
            _request(
                client, f"parallel-overlap-{index}", "parallel_overlap", "overlap",
                11 + index,
            ),
        )
        for index in range(concurrency)
    ]
    entered = await _wait_for_phase(provider, "overlap", concurrency)
    assert len(set(entered)) == concurrency
    assert provider.active == concurrency
    assert provider.peak_active == concurrency

    # The next request is refused at the shared ledger while all slots are held.
    with pytest.raises(RequestAdmissionError, match="run_llm_concurrency_unavailable"):
        await _request(client, "parallel-overlap-over-cap", "parallel_overlap", "overlap", 99)
    assert provider.active == concurrency
    provider.gates["overlap"].set()
    first_results = await asyncio.gather(*first_wave)
    assert {result.value["owner"] for result in first_results} == set(entered)

    # More callers race for the remaining budget units. Atomic SQLite
    # reservations admit one wave and reject the extra caller without provider I/O.
    budget_wave_size = concurrency
    budget_attempts = budget_wave_size + 1
    budget_owners = [f"parallel-budget-{index}" for index in range(budget_attempts)]
    budget_denials = asyncio.Queue()

    async def capture_budget_result(owner, index):
        try:
            return await _request(
                client, owner, "parallel_budget", "budget", 31 + index,
            )
        except RequestAdmissionError as exc:
            budget_denials.put_nowait((owner, exc))
            return exc

    budget_tasks = [
        asyncio.create_task(capture_budget_result(owner, index))
        for index, owner in enumerate(budget_owners)
    ]
    budget_entered = await _wait_for_phase(provider, "budget", budget_wave_size)
    assert len(set(budget_entered)) == budget_wave_size
    assert provider.active == budget_wave_size

    denied_owner, denied_error = await asyncio.wait_for(budget_denials.get(), 5)
    assert denied_owner not in budget_entered
    assert str(denied_error) == "run_llm_request_limit_exhausted"

    # Cancel one admitted provider call. It remains charged and consumes the
    # final budget unit as unknown usage after durable dispatch.
    cancelled_owner = budget_entered[-1]
    cancelled_task = budget_tasks[budget_owners.index(cancelled_owner)]
    cancelled_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled_task
    provider.gates["budget"].set()
    budget_results = await asyncio.gather(*budget_tasks, return_exceptions=True)
    budget_successes = [
        item for item in budget_results if isinstance(item, StructuredCompletion)
    ]
    budget_denials = [
        item for item in budget_results if isinstance(item, RequestAdmissionError)
    ]
    assert len(budget_successes) == budget_wave_size - 1
    assert len(budget_denials) == 1
    assert {str(item) for item in budget_denials} == {
        "run_llm_request_limit_exhausted",
    }
    assert {item.value["owner"] for item in budget_successes} == (
        set(budget_entered) - {cancelled_owner}
    )
    assert provider.active == 0

    expected_tokens = {
        f"parallel-overlap-{index}": 11 + index
        for index in range(concurrency)
    }
    expected_tokens.update({
        owner: 31 + int(owner.rsplit("-", 1)[1])
        for owner in budget_entered if owner != cancelled_owner
    })
    expected_owners = {
        *(f"parallel-overlap-{index}" for index in range(concurrency)),
        *budget_entered,
    }

    # Reopen both SQLite stores and bind the same immutable policy. The request
    # budget, ownership, stage and usage must come from durable records.
    reopened_tasks = SQLiteTaskGraphStore(state.db)
    reopened_paths = SQLitePathStore(state.db)
    reopened_coordinator = coordinator_for(reopened_tasks, reopened_paths)
    recovered = RunRequestLedger(reopened_coordinator)
    await recovered.bind(RUN, policy)
    snapshot = await recovered.snapshot(RUN)
    assert snapshot["spent"] == concurrency * 2
    assert snapshot["reserved"] == 0
    assert snapshot["known_tokens"] == sum(expected_tokens.values())
    assert snapshot["unknown_usage"] == snapshot["unknown_dispatch"] == 1
    assert snapshot["unknown_outcomes"] == {"cancelled": 1}
    assert snapshot["by_stage"]["parallel_overlap"]["spent"] == concurrency
    assert snapshot["by_stage"]["parallel_budget"]["spent"] == budget_wave_size
    assert snapshot["by_stage"]["parallel_budget"]["unknown_usage"] == 1

    rows = await _request_rows(reopened_coordinator)
    assert len(rows) == concurrency * 2
    rows_by_owner = {row["owner"]: row for row in rows}
    assert set(rows_by_owner) == expected_owners
    for owner, tokens in expected_tokens.items():
        row = rows_by_owner[owner]
        assert row["state"] == "settled"
        assert row["total_tokens"] == tokens
        assert owner in row["request_content"]
        assert owner in row["response_content"]
        assert row["stage"] == (
            "parallel_overlap" if owner.startswith("parallel-overlap-")
            else "parallel_budget"
        )
        assert provider.contexts[owner] == {
            "run_id": RUN,
            "owner": owner,
            "stage": row["stage"],
            "role_id": None,
            "phase": "overlap" if owner.startswith("parallel-overlap-") else "budget",
        }
    cancelled_row = rows_by_owner[cancelled_owner]
    assert cancelled_row["state"] == "unknown"
    assert cancelled_row["total_tokens"] is None
    assert cancelled_row["stage"] == "parallel_budget"
    assert provider.contexts[cancelled_owner] == {
        "run_id": RUN,
        "owner": cancelled_owner,
        "stage": "parallel_budget",
        "role_id": None,
        "phase": "budget",
    }

    # A fresh facade/store view still cannot overspend the exhausted Run budget.
    recovered_provider = BarrierProvider()
    recovered_client = RunMeteredLLM(recovered_provider, recovered)
    with pytest.raises(RequestAdmissionError, match="run_llm_request_limit_exhausted"):
        await _request(
            recovered_client, "parallel-after-reopen", "parallel_overlap", "overlap", 1,
        )
    assert recovered_provider.entered.empty()
    assert (await recovered.snapshot(RUN))["spent"] == concurrency * 2
