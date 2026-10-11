"""Malformed durable request records must not become spare budget (synthetic)."""

import pytest

from redteam.core.contracts.llm_policy import LLMRequestPolicy
from redteam.orchestration.llm_requests import RequestAdmissionError, RunRequestLedger
from tests.unit.test_orchestration_coordination import RUN, world


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
@pytest.mark.parametrize("mutation", [
    {"request_id": "different"}, {"owner": ""}, {"kind": ""}, {"state": "lost"},
    {"total_tokens": -1}, {"total_tokens": True}, {"total_tokens": 4},
])
async def test_invalid_row_cannot_admit_another_request(tmp_path, backend, mutation):
    w = await world(tmp_path, backend)
    ledger = RunRequestLedger(w.coordinator)
    await ledger.bind(RUN, LLMRequestPolicy(mode="unlimited_requests", max_concurrent_requests=2))
    request = await ledger.reserve(RUN, owner="worker", kind="native")
    async def corrupt(tx):
        tx.records[("llm_request", request)].update(mutation)
    await w.coordinator.change(corrupt)
    with pytest.raises(RequestAdmissionError, match="llm_request_record_invalid"):
        await ledger.reserve(RUN, owner="replacement", kind="structured")


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
async def test_request_index_mismatch_cannot_be_settled(tmp_path, backend):
    w = await world(tmp_path, backend)
    ledger = RunRequestLedger(w.coordinator)
    await ledger.bind(RUN, LLMRequestPolicy(mode="bounded", max_requests=1))
    request = await ledger.reserve(RUN, owner="worker", kind="native")
    await ledger.dispatch(RUN, request)
    async def corrupt(tx):
        tx.records[("llm_request", request)]["request_id"] = "different"
    await w.coordinator.change(corrupt)
    with pytest.raises(RequestAdmissionError, match="llm_request_record_invalid"):
        await ledger.settle(RUN, request, total_tokens=1)


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
@pytest.mark.parametrize("consumer", [
    "snapshot", "owner", "states", "launch", "dispatch", "release",
])
async def test_all_accounting_consumers_share_validation(tmp_path, backend, consumer):
    w = await world(tmp_path, backend)
    ledger = RunRequestLedger(w.coordinator)
    await ledger.bind(RUN, LLMRequestPolicy(mode="bounded", max_requests=2))
    request = await ledger.reserve(RUN, owner="original", kind="native")
    async def corrupt(tx):
        tx.records[("llm_request", request)]["state"] = ["not-a-state"]
    await w.coordinator.change(corrupt)
    async def read(tx):
        if consumer == "owner":
            return ledger.owner_has_activity(tx, RUN, "different-owner")
        if consumer == "states":
            return ledger.request_states(tx, RUN, owner="different-owner")
        return ledger.launch_blocked(tx, RUN)
    with pytest.raises(RequestAdmissionError, match="llm_request_record_invalid"):
        if consumer == "snapshot":
            await ledger.snapshot(RUN)
        elif consumer == "dispatch":
            await ledger.dispatch(RUN, request)
        elif consumer == "release":
            await ledger.release_undispatched(RUN, request)
        else:
            await w.coordinator.change(read)


@pytest.mark.parametrize("payload", [None, [], {}, {"run_id": ""}, {"run_id": True}])
async def test_unattributable_row_is_not_ignored(tmp_path, payload):
    w = await world(tmp_path, "memory")
    ledger = RunRequestLedger(w.coordinator)
    await ledger.bind(RUN, LLMRequestPolicy(mode="unlimited_requests"))
    async def corrupt(tx):
        tx.records[("llm_request", "unattributed")] = payload
    await w.coordinator.change(corrupt)
    with pytest.raises(RequestAdmissionError, match="llm_request_record_invalid"):
        await ledger.snapshot(RUN)


@pytest.mark.parametrize("field", ["request_id", "owner", "kind", "state", "total_tokens"])
async def test_missing_fields_do_not_become_zero_usage(tmp_path, field):
    w = await world(tmp_path, "memory")
    ledger = RunRequestLedger(w.coordinator)
    await ledger.bind(RUN, LLMRequestPolicy(mode="unlimited_requests"))
    request = await ledger.reserve(RUN, owner="original", kind="native")
    async def corrupt(tx):
        tx.records[("llm_request", request)].pop(field)
    await w.coordinator.change(corrupt)
    with pytest.raises(RequestAdmissionError, match="llm_request_record_invalid"):
        await ledger.snapshot(RUN)


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
async def test_valid_other_run_and_extension_kind_stay_isolated(tmp_path, backend):
    w = await world(tmp_path, backend)
    ledger = RunRequestLedger(w.coordinator)
    policy = LLMRequestPolicy(mode="unlimited_requests", max_concurrent_requests=2)
    await ledger.bind(RUN, policy)
    await ledger.bind("other-run", policy)
    other = await ledger.reserve("other-run", owner="other-owner", kind="extension_method_v2")
    await ledger.dispatch("other-run", other)
    await ledger.settle("other-run", other, total_tokens=9)
    request = await ledger.reserve(RUN, owner="original", kind="extension_method_v2")
    await ledger.dispatch(RUN, request)
    await ledger.settle(RUN, request, unknown=True)
    snapshot = await ledger.snapshot(RUN)
    assert snapshot["spent"] == snapshot["unknown_usage"] == snapshot["unknown_dispatch"] == 1
    assert snapshot["known_tokens"] == 0
    assert (await ledger.snapshot("other-run"))["known_tokens"] == 9
    with pytest.raises(RequestAdmissionError, match="identity_mismatch"):
        await ledger.release_undispatched(RUN, other)
    with pytest.raises(RequestAdmissionError, match="cannot_be_refunded"):
        await ledger.release_undispatched(RUN, request)


@pytest.mark.parametrize("fields", [
    {"run_id": ""}, {"owner": " "}, {"kind": ""}, {"owner": None}, {"kind": True},
])
async def test_invalid_scope_does_not_write_a_reservation(tmp_path, fields):
    w = await world(tmp_path, "memory")
    ledger = RunRequestLedger(w.coordinator)
    await ledger.bind(RUN, LLMRequestPolicy(mode="unlimited_requests"))
    with pytest.raises(RequestAdmissionError, match="llm_request_scope_invalid"):
        await ledger.reserve(**{"run_id": RUN, "owner": "original", "kind": "native", **fields})
    assert (await ledger.snapshot(RUN))["reserved"] == 0
