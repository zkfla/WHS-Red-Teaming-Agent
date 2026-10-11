"""Original-only recovery: synthetic provider, no model or target redispatch."""

import asyncio
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from redteam.core.contracts.execution import ExecutionRecoveryAnswer, ExecutionState
from redteam.core.convert import digest_payload
from redteam.core.ids import EvidenceId
from redteam.evidence.stores.filesystem import FilesystemEvidenceStore
from redteam.orchestration.execution_collection import collect_execution_originals
from redteam.orchestration.execution_journal import execution_processed
from redteam.orchestration.execution_reconciliation import reconcile_execution_receipts
from tests.integration.test_execution_receipt_reconciliation import _world


async def snapshots(deps, runtime):
    return (
        [item.model_dump(mode="json") for item in
         await deps.work_store.list_items(run_id=runtime.run_id)],
        [node.model_dump(mode="json") for node in await deps.graph_store.query()],
        runtime.model_dump(mode="json"),
    )


async def test_collection_preserves_late_original_once_without_processing_or_accounting(tmp_path):
    deps, provider, store, runtime, work = await _world(tmp_path, late=True)
    before_records = await store.list_for_run(runtime.run_id)
    before = await snapshots(deps, runtime)
    no_judgment = AsyncMock(side_effect=AssertionError("collection may not interpret originals"))
    deps = replace(deps, llm_client=no_judgment, verifier=no_judgment)
    result = await collect_execution_originals(deps, runtime)
    assert [row.state for row in result.originals] == ["known"]
    assert not result.replay_permitted and not result.claim_release_permitted
    assert provider.queries == provider.fetches == 1 and len(provider.invocations) == 1
    after_records = await store.list_for_run(runtime.run_id)
    assert len(after_records) == len(before_records) + 1
    assert after_records[-1].metadata["receipt_kind"] == "execution_resolution"
    assert not await execution_processed(store, after_records[-1])
    assert await snapshots(deps, runtime) == before
    for source in before_records:
        assert await store.get(source.evidence_id) == source
    repeated = await collect_execution_originals(deps, runtime)
    assert repeated == result and await store.list_for_run(runtime.run_id) == after_records
    assert provider.queries == provider.fetches == 1
    assert (await deps.work_store.get(work.work_id)).status == work.status
    assert "bounded preserved output" not in result.model_dump_json()


async def test_collection_then_normal_reconciliation_uses_same_original_not_provider(tmp_path):
    deps, provider, store, runtime, _work = await _world(tmp_path, late=True)
    collected = await collect_execution_originals(deps, runtime)
    assert collected.originals[0].state == "known"
    reconciled = await reconcile_execution_receipts(deps, runtime)
    assert reconciled.ready and reconciled.runtime.actions_executed == 1
    assert provider.queries == provider.fetches == 1 and len(provider.invocations) == 1
    assert await execution_processed(store, await store.get(
        EvidenceId(collected.originals[0].original_receipt_id),
    ))


@pytest.mark.parametrize("condition", [
    "no_provider", "running", "unknown", "unverified", "uncertain", "not_available",
    "missing_result", "uncertain_result", "query_error", "fetch_error", "query_timeout",
])
async def test_unresolved_original_never_replays_or_settles(tmp_path, condition):
    deps, provider, store, runtime, _work = await _world(tmp_path, late=True)
    if condition == "no_provider":
        deps = replace(deps, execution_recovery=None)
    elif condition in {"running", "unknown", "unverified", "uncertain", "not_available"}:
        values = {"state": ExecutionState.SUCCEEDED, "verified": True,
                  "uncertain": False, "result_available": True}
        if condition in {"running", "unknown"}:
            values["state"] = ExecutionState(condition)
        else:
            key = {"unverified": "verified", "uncertain": "uncertain",
                   "not_available": "result_available"}[condition]
            values[key] = condition == "uncertain"
        provider.query_execution = AsyncMock(return_value=ExecutionRecoveryAnswer(**values))
    elif condition in {"missing_result", "uncertain_result"}:
        provider.original = None if condition == "missing_result" else (
            provider.original.model_copy(update={"metadata": {"execution_uncertain": True}})
        )
    elif condition in {"query_error", "fetch_error"}:
        setattr(provider, "query_execution" if condition == "query_error" else "fetch_execution",
                AsyncMock(side_effect=OSError("sensitive original body must not be exposed")))
    else:
        async def hung(reference):
            raise TimeoutError("sensitive provider endpoint")
        provider.query_execution = hung
    before = await snapshots(deps, runtime)
    records = await store.list_for_run(runtime.run_id)
    result = await collect_execution_originals(deps, runtime)
    assert result.originals[0].state == "held"
    assert await snapshots(deps, runtime) == before
    assert await store.list_for_run(runtime.run_id) == records
    assert len(provider.invocations) == 1
    assert "sensitive" not in result.model_dump_json()


@pytest.mark.parametrize("field", ["invocation_id", "action_id", "tool_id"])
async def test_foreign_provider_result_is_invalid_before_append(tmp_path, field):
    deps, provider, store, runtime, _work = await _world(tmp_path, late=True)
    provider.original = provider.original.model_copy(update={field: "foreign"})
    before = await store.list_for_run(runtime.run_id)
    result = await collect_execution_originals(deps, runtime)
    assert result.originals[0].state == "invalid"
    assert result.originals[0].reason_code == "execution_receipt_result_identity_mismatch"
    assert await store.list_for_run(runtime.run_id) == before
    assert len(provider.invocations) == 1


@pytest.mark.parametrize("field", [
    "engagement_id", "run_id", "instance_id", "action_id", "invocation_id",
    "tool_id", "input_digest",
])
async def test_provider_reference_input_and_identity_verified_before_query(tmp_path, field):
    deps, provider, store, runtime, _work = await _world(tmp_path, late=True)
    prepare = provider.prepare_reference
    provider.prepare_reference = lambda call: prepare(call).model_copy(update={
        field: "0" * 64 if field == "input_digest" else "foreign",
    })
    result = await collect_execution_originals(deps, runtime)
    assert result.originals[0].state == "invalid"
    assert result.originals[0].reason_code == "execution_recovery_input_mismatch"
    assert provider.queries == provider.fetches == 0


@pytest.mark.parametrize("condition", ["digest", "conflict", "task", "work"])
async def test_corrupt_or_unmatched_inventory_rejected_before_provider(tmp_path, condition):
    deps, provider, store, runtime, work = await _world(tmp_path, late=True)
    if condition == "task":
        result = await collect_execution_originals(deps, runtime, original_task_id="foreign-task")
    else:
        if condition == "work":
            await deps.work_store.add(work.model_copy(update={
                "candidate_key": "foreign", "work_id": work.work_id + "-other",
            }))
            submission = next(r for r in await store.list_for_run(runtime.run_id)
                              if r.metadata.get("receipt_kind") == "execution_submission")
            bad = FilesystemEvidenceStore(tmp_path / "bad")
            altered = submission.model_copy(deep=True)
            altered.metadata["work_id"] = work.work_id + "-other"
            altered.digest = digest_payload(altered.metadata)
            await bad.append(altered)
            deps = replace(deps, evidence_store=bad)
        else:
            bad = FilesystemEvidenceStore(tmp_path / "bad")
            for source in await store.list_for_run(runtime.run_id):
                altered = source.model_copy(deep=True)
                if altered.metadata.get("receipt_kind") == "execution_submission":
                    altered.metadata["proposal"]["reason"] = "conflicting submission"
                    if condition == "conflict":
                        await bad.append(source)
                        altered.evidence_id = EvidenceId("conflicting-submission")
                        altered.digest = digest_payload(altered.metadata)
                await bad.append(altered)
            deps = replace(deps, evidence_store=bad)
        result = await collect_execution_originals(deps, runtime)
    assert result.originals[0].state == "invalid"
    assert provider.queries == provider.fetches == 0 and len(provider.invocations) == 1


async def test_cancel_during_original_query_preserves_every_marker(tmp_path):
    deps, provider, store, runtime, _work = await _world(tmp_path, late=True)
    provider.query_execution = AsyncMock(side_effect=asyncio.CancelledError)
    before = await snapshots(deps, runtime)
    records = await store.list_for_run(runtime.run_id)
    with pytest.raises(asyncio.CancelledError):
        await collect_execution_originals(deps, runtime)
    assert await snapshots(deps, runtime) == before
    assert await store.list_for_run(runtime.run_id) == records
    assert len(provider.invocations) == 1


async def test_other_instance_is_not_queried_and_empty_does_not_authorize_resume(tmp_path):
    deps, provider, _store, runtime, _work = await _world(tmp_path, late=True)
    result = await collect_execution_originals(
        deps, runtime.model_copy(update={"instance_id": "other-instance"}),
    )
    assert result.originals == []
    assert not result.replay_permitted and not result.claim_release_permitted
    assert provider.queries == provider.fetches == 0


@pytest.mark.parametrize("stage", ["query", "fetch"])
async def test_hanging_read_is_cancelled_at_original_configured_deadline(tmp_path, stage):
    deps, provider, store, runtime, _work = await _world(tmp_path, late=True)
    bounded = FilesystemEvidenceStore(tmp_path / "bounded")
    for source in await store.list_for_run(runtime.run_id):
        row = source.model_copy(deep=True)
        if row.metadata.get("receipt_kind") in {"execution_submission", "execution_result"}:
            row.metadata["invocation"]["timeout_seconds"] = 0.02
            row.digest = digest_payload(row.metadata)
        await bounded.append(row)
    deps = replace(deps, evidence_store=bounded)
    cancelled = asyncio.Event()
    async def hung(reference):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
    setattr(provider, "query_execution" if stage == "query" else "fetch_execution", hung)
    result = await asyncio.wait_for(collect_execution_originals(deps, runtime), 2)
    assert result.originals[0].state == "held"
    assert result.originals[0].reason_code == "TimeoutError" and cancelled.is_set()
    assert len(provider.invocations) == 1
    assert not any(row.metadata.get("receipt_kind") == "execution_resolution"
                   for row in await bounded.list_for_run(runtime.run_id))


async def test_resolution_ack_loss_is_read_next_time_not_fetched_or_appended_twice(
    tmp_path, monkeypatch,
):
    deps, provider, store, runtime, _work = await _world(tmp_path, late=True)
    append = store.append
    async def lost_ack(record):
        await append(record)
        if record.metadata.get("receipt_kind") == "execution_resolution":
            raise OSError("sensitive lost acknowledgement")
    monkeypatch.setattr(store, "append", lost_ack)
    first = await collect_execution_originals(deps, runtime)
    assert first.originals[0].state == "held"
    second = await collect_execution_originals(deps, runtime)
    assert second.originals[0].state == "known"
    assert provider.queries == provider.fetches == 1 and len(provider.invocations) == 1
    resolutions = [row for row in await store.list_for_run(runtime.run_id)
                   if row.metadata.get("receipt_kind") == "execution_resolution"]
    assert len(resolutions) == 1 and not await execution_processed(store, resolutions[0])


async def test_foreign_embedded_execution_context_is_rejected_before_provider(tmp_path):
    deps, provider, store, runtime, _work = await _world(tmp_path, late=True)
    bad = FilesystemEvidenceStore(tmp_path / "foreign-context")
    source = next(row for row in await store.list_for_run(runtime.run_id)
                  if row.metadata.get("receipt_kind") == "execution_submission")
    source = source.model_copy(deep=True)
    source.metadata["invocation"]["execution_context"]["run_id"] = "foreign-run"
    source.digest = digest_payload(source.metadata)
    await bad.append(source)
    result = await collect_execution_originals(replace(deps, evidence_store=bad), runtime)
    assert result.originals[0].state == "invalid"
    assert result.originals[0].reason_code == "execution_receipt_identity_mismatch"
    assert provider.queries == provider.fetches == 0


async def test_known_original_does_not_release_resource_execution_marker(tmp_path):
    deps, _provider, store, runtime, work = await _world(tmp_path, late=True)
    guarded_work = work.model_copy(update={
        "work_id": work.work_id + "-guarded", "resource_keys": ["browser:synthetic"],
        "conflict_keys": [*work.conflict_keys, "browser:synthetic"],
        "resource_execution_pending": True,
    })
    await deps.work_store.add(guarded_work)
    guarded = FilesystemEvidenceStore(tmp_path / "guarded")
    for source in await store.list_for_run(runtime.run_id):
        row = source.model_copy(deep=True)
        if row.metadata.get("receipt_kind") in {"execution_submission", "execution_result"}:
            row.metadata["work_id"] = guarded_work.work_id
            row.digest = digest_payload(row.metadata)
        await guarded.append(row)
    result = await collect_execution_originals(replace(deps, evidence_store=guarded), runtime)
    assert result.originals[0].state == "known"
    assert await deps.work_store.get(guarded_work.work_id) == guarded_work
