"""Returned provider envelopes can release capacity only with bound evidence."""

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from redteam.core.contracts.action import ActionProposal
from redteam.core.contracts.execution import ExecutionState
from redteam.core.contracts.tool import ToolInvocation, ToolResult
from redteam.evidence.stores.filesystem import FilesystemEvidenceStore
from redteam.orchestration import central_recovery
from redteam.orchestration.central_completion import unsafe
from redteam.orchestration.central_state import (
    CheckedEvidence,
    check_evidence,
    execution_occupies_capacity,
)
from redteam.orchestration.coordination import StateConflict
from redteam.orchestration.execution_collection import collect_execution_termination
from redteam.orchestration.execution_journal import (
    ExecutionReceiptError,
    append_execution_receipt,
    append_execution_termination,
    checked_receipt,
)
from redteam.orchestration.state import RuntimeState
from tests.unit.test_central_state import ENG, RUN, execution, original, owner, world

NOW = datetime(2026, 10, 16, tzinfo=timezone.utc)
GOOD_METADATA = {
    "execution_started": True,
    "execution_uncertain": True,
    "execution_terminated": True,
}


async def add_result(store, submission, *, metadata=None, output=None, invocation=None,
                     step=None, work_id=None):
    data = checked_receipt(submission)
    invocation = invocation or ToolInvocation.model_validate(data["invocation"])
    proposal = ActionProposal.model_validate(data["proposal"])
    result = ToolResult(
        invocation_id=invocation.invocation_id,
        action_id=invocation.action_id,
        tool_id=invocation.tool_id,
        success=True,
        output={"provider_claim": "all done", "payload": output or {"raw": "DATA"}},
        started_at=NOW,
        finished_at=NOW,
        metadata=dict(GOOD_METADATA if metadata is None else metadata),
    )
    return await append_execution_receipt(
        store,
        invocation=invocation,
        proposal=proposal,
        step=step or data_step(data),
        work_id=work_id,
        result=result,
    )


def data_step(data):
    from redteam.orchestration.agents.base import PlanStep

    return PlanStep.model_validate(data["plan_step"])


async def setup_returned_unknown(tmp_path, *, metadata=None, output=None):
    state = await world(tmp_path)
    await owner(state)
    seed = FilesystemEvidenceStore(tmp_path / "seed")
    source, = await original(seed, "proposal-a")
    # This is the real submission receipt, deliberately without a provider
    # execution reference. The returned envelope must stand on its own proof.
    submission = source.record
    store = FilesystemEvidenceStore(tmp_path / "evidence")
    await store.append(submission)
    checked = await check_evidence(
        store, run_id=RUN, engagement_id=ENG,
        evidence_ids=[str(submission.evidence_id)],
    )
    result_record = await add_result(store, submission, metadata=metadata, output=output)
    intent = execution(proposal_evidence_id=submission.evidence_id)
    await state.transaction(lambda tx: tx.record_execution(
        intent, owner="worker-a", checked=checked, now=100,
    ))
    entered = await state.transaction(lambda tx: tx.update_execution(
        intent.model_copy(update={"state": ExecutionState.RUNNING}),
        owner="worker-a", now=100,
    ))
    unknown = await state.transaction(lambda tx: tx.update_execution(
        entered.model_copy(update={"state": ExecutionState.CANCELLED}),
        owner="worker-a", now=500,
    ))
    return state, store, submission, checked[0], result_record, unknown


def recovery_runtime():
    return RuntimeState(
        run_id=RUN,
        engagement_id=ENG,
        mission_id="mission-central",
        instance_id="instance-a",
        task_owner="worker-a",
        task_ownership_version=1,
        active_task_id="task-a",
        current_agent="web",
    )


async def bypass_fixture_binding(_deps, _runtime, *, require_task=True, admit=True):
    # The shared central-state fixture uses a fixed test sentinel; retain its
    # actual SQLite owner/CAS path while avoiding the unrelated binding setup.
    return None


@pytest.mark.asyncio
async def test_returned_result_reconciles_offline_and_keeps_unknown_resource_fences(
    tmp_path, monkeypatch,
):
    state, store, submission, _, result_record, unknown = await setup_returned_unknown(
        tmp_path, output={"execution_terminated": True, "message": "untrusted DATA"},
    )
    calls = []

    class MustNotCallProvider:
        def prepare_reference(self, invocation):
            calls.append(("prepare", invocation))
            pytest.fail("original-result termination must not fabricate a provider reference")

        async def query_execution(self, reference):
            calls.append(("query", reference))
            pytest.fail("original-result termination must not query the provider")

    monkeypatch.setattr(central_recovery, "runtime_token", bypass_fixture_binding)
    deps = SimpleNamespace(
        central_state=state,
        evidence_store=store,
        execution_recovery=MustNotCallProvider(),
        long_job_adapter=None,
    )
    outcome = await central_recovery.reconcile_originals(deps, recovery_runtime())

    assert outcome.errors == {}
    assert outcome.pending_invocations == ["invocation-a"]
    assert calls == []
    released = await state.get_execution("invocation-a")
    proof_id = released.termination_evidence_id
    assert proof_id is not None
    assert released.state is ExecutionState.UNKNOWN
    assert released.result_evidence_id is None
    assert released.resource_execution_pending
    assert released.resource_keys == unknown.resource_keys
    assert unsafe(released)
    assert not execution_occupies_capacity(released)
    proof = await store.get(proof_id)
    proof_data = checked_receipt(proof)
    assert proof_data["termination_basis"] == "original_result"
    assert proof_data["termination_original_id"] == str(result_record.evidence_id)
    assert proof_data["termination_original_digest"] == result_record.digest
    assert "execution_reference" not in proof_data

    independent = execution(invocation_id="independent", resource_keys=["session-b"])
    await state.transaction(lambda tx: tx.check_execution_capacity(
        independent, max_calls=1, per_target=1,
    ))
    with pytest.raises(StateConflict, match="shared_resource_execution_conflict"):
        await state.transaction(lambda tx: tx._resource_conflict(
            independent.model_copy(update={"resource_keys": unknown.resource_keys}), now=99_999,
        ))
    with pytest.raises(StateConflict, match="unknown_execution_cannot_be_replayed"):
        await state.transaction(lambda tx: tx.update_execution(
            released.model_copy(update={"state": ExecutionState.RUNNING}),
            owner="worker-a", now=500,
        ))

    restarted_state = type(state)(state.path, run_id=RUN, engagement_id=ENG)
    await restarted_state.setup()
    restarted_store = FilesystemEvidenceStore(tmp_path / "evidence")
    resumed = await central_recovery.reconcile_originals(
        SimpleNamespace(
            central_state=restarted_state,
            evidence_store=restarted_store,
            execution_recovery=MustNotCallProvider(),
            long_job_adapter=None,
        ),
        recovery_runtime(),
    )
    assert resumed.errors == {}
    assert resumed.pending_invocations == ["invocation-a"]
    persisted = await restarted_state.get_execution("invocation-a")
    assert persisted.termination_evidence_id == proof_id
    assert persisted.state is ExecutionState.UNKNOWN and unsafe(persisted)
    assert persisted.resource_execution_pending
    assert "execution_reference" not in checked_receipt(await restarted_store.get(proof_id))
    terminations = [
        record for record in await restarted_store.list_for_action(submission.action_id)
        if record.metadata.get("receipt_kind") == "execution_termination"
    ]
    assert len(terminations) == 1
    assert calls == []


@pytest.mark.parametrize(
    "metadata",
    [
        {"execution_started": True, "execution_uncertain": True},
        {"execution_started": True, "execution_uncertain": True,
         "execution_terminated": "true"},
        {"execution_started": True, "execution_uncertain": True,
         "execution_terminated": 1},
        {"execution_started": True, "execution_uncertain": True,
         "execution_terminated": False},
        {"execution_started": False, "execution_uncertain": True,
         "execution_terminated": True},
    ],
)
@pytest.mark.asyncio
async def test_returned_result_without_strict_termination_assertion_does_not_release(
    tmp_path, metadata,
):
    state, store, submission, _, _, unknown = await setup_returned_unknown(
        tmp_path, metadata=metadata,
        output={"execution_started": True, "execution_terminated": True,
                "message": "provider says this completed"},
    )
    deps = SimpleNamespace(evidence_store=store, execution_recovery=None)
    if metadata.get("execution_terminated") is True:
        with pytest.raises(
            ExecutionReceiptError, match="execution_termination_original_unverified",
        ):
            await collect_execution_termination(deps, submission)
    else:
        assert await collect_execution_termination(deps, submission) is None
    assert execution_occupies_capacity(await state.get_execution("invocation-a"))
    assert unknown.termination_evidence_id is None


@pytest.mark.parametrize("mismatch", ["stale_work", "wrong_invocation", "wrong_purpose"])
@pytest.mark.asyncio
async def test_termination_rejects_original_receipt_outside_exact_submission_binding(
    tmp_path, mismatch,
):
    _, _, submission, _, _, _ = await setup_returned_unknown(tmp_path)
    data = checked_receipt(submission)
    invocation = ToolInvocation.model_validate(data["invocation"])
    step = data_step(data)
    work_id = None
    if mismatch == "stale_work":
        work_id = "previous-work"
    elif mismatch == "wrong_invocation":
        invocation = invocation.model_copy(update={"invocation_id": "invocation-stale"})
    else:
        step = step.model_copy(update={"task_purpose_hash": "c" * 64})
    detached_store = FilesystemEvidenceStore(tmp_path / f"detached-{mismatch}")
    detached = await add_result(
        detached_store, submission, invocation=invocation, step=step, work_id=work_id,
    )
    with pytest.raises(ExecutionReceiptError, match="execution_termination_original_mismatch"):
        await append_execution_termination(
            FilesystemEvidenceStore(tmp_path / "proofs"), submission,
            ceased_answer(), original=detached,
        )


def ceased_answer():
    from redteam.core.contracts.execution import ExecutionRecoveryAnswer

    return ExecutionRecoveryAnswer(
        state=ExecutionState.UNKNOWN, verified=True, uncertain=True,
        execution_terminated=True,
    )


@pytest.mark.asyncio
async def test_original_basis_requires_real_original_and_revalidates_its_digest(tmp_path):
    state, store, submission, checked_submission, original_result, _ = (
        await setup_returned_unknown(tmp_path)
    )
    with pytest.raises(ExecutionReceiptError, match="execution_termination_unverified"):
        await append_execution_termination(store, submission, ceased_answer())

    proof = await append_execution_termination(
        store, submission, ceased_answer(), original=original_result,
    )
    proof_checked, = await check_evidence(
        store, run_id=RUN, engagement_id=ENG, evidence_ids=[str(proof.evidence_id)],
    )
    with pytest.raises(StateConflict, match="execution_termination_original_missing"):
        await state.transaction(lambda tx: tx.record_execution_termination(
            "invocation-a", submission=checked_submission, proof=proof_checked,
            owner="worker-a", version=1,
        ))

    corrupted = original_result.model_copy(update={"digest": "f" * 64})
    with pytest.raises(ExecutionReceiptError, match="execution_receipt_digest_mismatch"):
        await state.transaction(lambda tx: tx.record_execution_termination(
            "invocation-a", submission=checked_submission, proof=proof_checked,
            original_result=CheckedEvidence(corrupted, checked_submission.content_hash),
            owner="worker-a", version=1,
        ))
    assert await state.get_execution("invocation-a") is not None


@pytest.mark.asyncio
async def test_termination_rejects_proof_bound_to_another_original_or_stale_execution_pointer(
    tmp_path,
):
    state, store, submission, checked_submission, result_a, unknown = (
        await setup_returned_unknown(tmp_path)
    )
    result_b = await add_result(
        FilesystemEvidenceStore(tmp_path / "alternate-original"), submission,
        output={"different": "DATA"},
    )
    proof_b = await append_execution_termination(
        store, submission, ceased_answer(), original=result_b,
    )
    checked_proof_b, = await check_evidence(
        store, run_id=RUN, engagement_id=ENG, evidence_ids=[str(proof_b.evidence_id)],
    )

    # Simulate an inconsistent persisted pointer. The termination transaction
    # must still bind the proof to the execution's already-recorded original.
    mismatched_execution = unknown.model_copy(update={"result_evidence_id": result_a.evidence_id})
    await state.transaction(lambda tx: tx.db.execute(
        "UPDATE state_executions SET payload=? WHERE run_id=? AND invocation_id=?",
        (mismatched_execution.model_dump_json(), RUN, "invocation-a"),
    ))
    checked_result_b = (await check_evidence(
        FilesystemEvidenceStore(tmp_path / "alternate-original"), run_id=RUN,
        engagement_id=ENG, evidence_ids=[str(result_b.evidence_id)],
    ))[0]
    with pytest.raises(StateConflict, match="execution_termination_original_mismatch"):
        await state.transaction(lambda tx: tx.record_execution_termination(
            "invocation-a", submission=checked_submission, proof=checked_proof_b,
            original_result=checked_result_b, owner="worker-a", version=1,
        ))
    assert (await state.get_execution("invocation-a")).termination_evidence_id is None
