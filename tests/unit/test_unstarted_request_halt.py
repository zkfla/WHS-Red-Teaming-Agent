"""Offline request admission: a stale queued attempt never becomes provider usage."""

import asyncio

import pytest

from redteam.core.ports.llm import StructuredCompletion
from redteam.orchestration.cognition import (
    DecisionBudget,
    StructuredDecisionClient,
    UnstartedRequestHalt,
)
from redteam.orchestration.metered_llm import llm_request_scope
from tests.unit.test_orchestration_coordination import RUN
from tests.unit.test_plan4c_time_admission import metered_client

SCHEMA = {"type": "object", "properties": {"ok": {"const": True}}, "required": ["ok"]}


class QueuedSemaphore(asyncio.Semaphore):
    def __init__(self, block_at):
        super().__init__(1)
        self.block_at = block_at
        self.acquires = 0
        self.waiting = asyncio.Event()
        self.proceed = asyncio.Event()

    async def acquire(self):
        self.acquires += 1
        if self.acquires == self.block_at:
            self.waiting.set()
            await self.proceed.wait()
        return await super().acquire()


class Provider:
    def __init__(self, valid):
        self.valid = valid
        self.calls = 0

    async def complete_structured_with_usage(self, **kwargs):
        self.calls += 1
        return StructuredCompletion(value={"ok": self.valid}, total_tokens=3)


@pytest.mark.parametrize("repair", [False, True])
async def test_stale_after_slot_wait_releases_only_unstarted_attempt(tmp_path, repair):
    provider = Provider(valid=not repair)
    metered = await metered_client(tmp_path, "memory", provider)
    semaphore = QueuedSemaphore(block_at=2 if repair else 1)
    metered.request_semaphore = semaphore
    budget = DecisionBudget.reasoning_chain(2, 2)
    client = StructuredDecisionClient(metered)
    stale = False

    async def guard():
        if stale:
            raise UnstartedRequestHalt("related_inputs_changed")

    async def ask():
        with llm_request_scope(RUN, "offline-freshness"):
            return await client.ask(stage="freshness", payload={}, response_schema=SCHEMA,
                                    budget=budget, before_call=guard)

    queued = asyncio.create_task(ask())
    try:
        await asyncio.wait_for(semaphore.waiting.wait(), 3)
        stale = True
        semaphore.proceed.set()
        with pytest.raises(UnstartedRequestHalt):
            await asyncio.wait_for(queued, 3)
    finally:
        semaphore.proceed.set()
        if not queued.done():
            queued.cancel()
        await asyncio.gather(queued, return_exceptions=True)

    spent = int(repair)
    usage = await metered.ledger.snapshot(RUN)
    assert provider.calls == spent
    assert usage["spent"] == spent and usage["reserved"] == 0
    assert usage["unknown_usage"] == usage["unknown_dispatch"] == 0
    assert budget.used == budget.normal_used == spent
    assert budget.repair_used == budget.transport_used == 0
    assert budget.tokens_used == 3 * spent and budget.unknown_usage_count == 0
    assert budget.last_request_id is None

    # The same ledger and remaining decision allowance stay usable. The first
    # dispatched, rejected response remains charged when the next request succeeds.
    stale = False
    provider.valid = True
    assert await ask() == {"ok": True}
    assert provider.calls == spent + 1
    usage = await metered.ledger.snapshot(RUN)
    assert usage["spent"] == spent + 1 and usage["reserved"] == 0
    assert budget.used == spent + 1 and budget.repair_used == 0


async def test_owner_halt_before_attempt_keeps_budget_empty(tmp_path):
    provider = Provider(valid=True)
    metered = await metered_client(tmp_path, "memory", provider)
    budget = DecisionBudget.reasoning_chain(2, 2)

    async def deny():
        raise UnstartedRequestHalt("related_inputs_changed")

    with llm_request_scope(RUN, "offline-freshness"), pytest.raises(UnstartedRequestHalt):
        await StructuredDecisionClient(metered).ask(
            stage="freshness", payload={}, response_schema=SCHEMA,
            budget=budget, before_call=deny,
        )
    usage = await metered.ledger.snapshot(RUN)
    assert provider.calls == usage["spent"] == usage["reserved"] == budget.used == 0
